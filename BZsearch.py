import os
import json
import re
import time
import asyncio
import difflib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from urllib.parse import quote
import random
import string

from hoshino import Service, priv
from hoshino.typing import CQEvent
import aiohttp

# 主服务定义
sv = Service('b站视频搜索', enable_on_default=True, help_='搜索B站视频\n使用方法：\n1. 查视频 [关键词] - 搜索B站视频\n2. 查up [UP主名称] - 搜索UP主视频\n3. 关注up [UP主名称] - 监控UP主新视频\n4. 视频关注 [视频链接] - 通过视频链接关注UP主\n5. 取关up [UP主名称] - 取消监控\n6. 查看关注 - 查看当前监控列表')

# 配置项
MAX_RESULTS = 5
UP_WATCH_INTERVAL = 10
CACHE_EXPIRE_MINUTES = 3
search_cache = {}

# JSON存储文件路径
WATCH_JSON_PATH = Path(__file__).parent / 'data' / 'bili_watch.json'
os.makedirs(WATCH_JSON_PATH.parent, exist_ok=True)

class UpWatchStorage:
    def __init__(self):
        self._data = {}
        self.name_index = {}  # 名称小写索引
        self._load_and_migrate()
    
    def _load_and_migrate(self):
        """加载并自动迁移旧格式数据"""
        try:
            if WATCH_JSON_PATH.exists():
                with open(WATCH_JSON_PATH, 'r', encoding='utf-8') as f:
                    old_data = json.load(f)
                    
                    # 检查是否为旧格式(包含数字UID键)
                    is_old_format = any(
                        any(k.isdigit() for k in ups.keys())
                        for ups in old_data.values()
                    )
                    
                    if is_old_format:
                        sv.logger.info("检测到旧格式数据，开始自动迁移...")
                        self._migrate_from_old_format(old_data)
                    else:
                        self._data = old_data
                        # 重建名称索引
                        for group_id, ups in old_data.items():
                            for up_name in ups.keys():
                                self.name_index[up_name.lower()] = (group_id, up_name)
                        
        except Exception as e:
            sv.logger.error(f"加载监控数据失败: {str(e)}")
            self._data = {}
    
    def _migrate_from_old_format(self, old_data):
        """从旧格式迁移数据"""
        migrated_count = 0
        for group_id, ups in old_data.items():
            self._data[group_id] = {}
            for uid, info in ups.items():
                try:
                    up_name = info['up_name']
                    # 确保名称唯一性
                    if up_name.lower() in self.name_index:
                        sv.logger.warning(f"发现重复UP主名称: {up_name}，添加随机后缀")
                        up_name = f"{up_name}_{uid[-4:]}"
                    
                    self._data[group_id][up_name] = {
                        'last_check': info['last_check'],
                        'last_vid': info['last_vid']
                    }
                    self.name_index[up_name.lower()] = (group_id, up_name)
                    migrated_count += 1
                except Exception as e:
                    sv.logger.error(f"迁移UP主 {uid} 失败: {str(e)}")
        
        sv.logger.info(f"数据迁移完成，共迁移 {migrated_count} 条记录")
        self.save()  # 立即保存新格式
    
    def save(self):
        try:
            with open(WATCH_JSON_PATH, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            sv.logger.error(f"保存监控数据失败: {str(e)}")
    
    def add_watch(self, group_id: int, up_name: str, last_vid: str = None):
        group_id = str(group_id)
        if group_id not in self._data:
            self._data[group_id] = {}
        
        # 标准化名称存储
        self._data[group_id][up_name] = {
            'last_check': datetime.now().isoformat(),
            'last_vid': last_vid
        }
        self.name_index[up_name.lower()] = (group_id, up_name)
        self.save()
    
    def remove_watch(self, group_id: int, up_name: str) -> bool:
        group_id = str(group_id)
        if group_id in self._data and up_name in self._data[group_id]:
            del self._data[group_id][up_name]
            if up_name.lower() in self.name_index:
                del self.name_index[up_name.lower()]
            if not self._data[group_id]:
                del self._data[group_id]
            self.save()
            return True
        return False
    
    def get_group_watches(self, group_id: int) -> Dict[str, Dict[str, Any]]:
        group_id = str(group_id)
        return self._data.get(group_id, {})
    
    def get_all_watches(self) -> Dict[str, Any]:
        return self._data
    
    def update_last_video(self, group_id: int, up_name: str, last_vid: str):
        group_id = str(group_id)
        if group_id in self._data and up_name in self._data[group_id]:
            self._data[group_id][up_name].update({
                'last_vid': last_vid,
                'last_check': datetime.now().isoformat()
            })
            self.save()
    
    def find_up_by_name(self, name: str) -> Optional[Tuple[str, str]]:
        """通过名称查找(不区分大小写)"""
        return self.name_index.get(normalize_name(name))

# 全局存储实例
watch_storage = UpWatchStorage()

def normalize_name(name: str) -> str:
    """标准化名称(去前后空格/小写)"""
    return name.strip().lower()

async def get_bilibili_search(keyword: str, search_type: str = "video") -> list:
    cache_key = f"{search_type}:{normalize_name(keyword)}"
    if cache_key in search_cache:
        cached_data, timestamp = search_cache[cache_key]
        if datetime.now() - timestamp < timedelta(minutes=CACHE_EXPIRE_MINUTES):
            return cached_data[:MAX_RESULTS]

    params = {
        'search_type': 'video',
        'keyword': keyword,
        'order': 'pubdate' if search_type == "up" else 'totalrank',
        'ps': MAX_RESULTS,
        'platform': 'web'
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Cookie': 'buvid3=XXXXXX'
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                'https://api.bilibili.com/x/web-interface/search/type',
                params=params,
                headers=headers,
                timeout=10
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                if data.get('code') == 0:
                    results = data['data'].get('result', [])[:MAX_RESULTS]
                    if search_type == "up":
                        # 确保结果中包含作者名字段
                        results = [v for v in results if 'author' in v]
                    search_cache[cache_key] = (results, datetime.now())
                    return results
        except Exception as e:
            sv.logger.error(f"搜索失败: {str(e)}")
        return []

async def get_video_info(bvid: str) -> Optional[Dict]:
    """获取视频详细信息"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
    }
    url = f'https://api.bilibili.com/x/web-interface/view?bvid={bvid}'
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if data.get('code') == 0:
                    return data['data']
        except Exception:
            return None
    return None

async def safe_send(bot, ev, message):
    """安全发送消息"""
    try:
        if not message or message.strip() == '':
            return
            
        if isinstance(message, list):
            message = '\n'.join(message)
            
        await bot.send(ev, message)
    except Exception as e:
        sv.logger.error(f'发送消息失败: {str(e)}')



@sv.on_prefix('关注up')
async def watch_bilibili_up(bot, ev: CQEvent):
    up_name = ev.message.extract_plain_text().strip()
    if not up_name:
        await bot.send(ev, '请输入UP主名称，例如：关注up 老番茄')
        return
    
    group_id = ev.group_id
    
    # 检查是否已关注
    if watch_storage.find_up_by_name(up_name):
        await bot.send(ev, f'【{up_name}】已在监控列表中')
        return
    
    # 搜索UP主最新视频
    results = await get_bilibili_search(up_name, "up")
    if not results:
        await bot.send(ev, f'未找到UP主【{up_name}】的视频')
        return
    
    # 精确匹配名称
    exact_match = next(
        (v for v in results 
         if normalize_name(v['author']) == normalize_name(up_name)),
        None
    )
    
    if not exact_match:
        await bot.send(ev, f'未找到名称完全匹配的UP主【{up_name}】')
        return
    
    # 添加到监控
    watch_storage.add_watch(
        group_id=group_id,
        up_name=exact_match['author'],  # 使用API返回的标准名称
        last_vid=exact_match['bvid']
    )
    
    await bot.send(ev, f'✅ 已关注UP主【{exact_match["author"]}】\n'
                      f'最新视频: {exact_match["title"]}\n'
                      '将监控后续更新')

@sv.on_prefix('视频关注')
async def watch_by_video(bot, ev: CQEvent):
    """通过视频链接关注UP主"""
    video_url = ev.message.extract_plain_text().strip()
    if not video_url:
        await bot.send(ev, '请输入视频链接，例如：视频关注 https://www.bilibili.com/video/BV1xxx')
        return
    
    # 提取BV号
    bvid = None
    if 'bilibili.com/video/' in video_url:
        match = re.search(r'bilibili\.com/video/(BV[0-9A-Za-z]+)', video_url)
        if match:
            bvid = match.group(1)
    
    if not bvid:
        await bot.send(ev, '无法从链接中识别视频BV号，请确认链接格式正确')
        return
    
    group_id = ev.group_id
    
    try:
        # 获取视频信息
        video_info = await get_video_info(bvid)
        if not video_info:
            await bot.send(ev, '获取视频信息失败，请稍后再试')
            return
        
        up_name = video_info['owner']['name']
        
        # 检查是否已关注
        if watch_storage.find_up_by_name(up_name):
            await bot.send(ev, f'【{up_name}】已在监控列表中')
            return
        
        # 添加到监控
        watch_storage.add_watch(
            group_id=group_id,
            up_name=up_name,
            last_vid=bvid
        )
        
        await bot.send(ev, f'✅ 已通过视频关注UP主【{up_name}】\n'
                         f'视频标题: {video_info["title"]}\n'
                         '将监控后续更新')
        
    except Exception as e:
        await bot.send(ev, f'通过视频关注失败: {str(e)}')

@sv.on_prefix('取关up')
async def unwatch_bilibili_up(bot, ev: CQEvent):
    up_name = ev.message.extract_plain_text().strip()
    group_id = ev.group_id
    
    # 查找准确的UP主名称
    found = watch_storage.find_up_by_name(up_name)
    if not found:
        await bot.send(ev, f'未找到【{up_name}】的监控记录')
        return
    
    group_id_str, exact_name = found
    if watch_storage.remove_watch(int(group_id_str), exact_name):
        await bot.send(ev, f'✅ 已取消对【{exact_name}】的监控')
    else:
        await bot.send(ev, '取关失败，请稍后再试')

@sv.on_fullmatch('查看关注')
async def list_watched_ups(bot, ev: CQEvent):
    group_id = ev.group_id
    watches = watch_storage.get_group_watches(group_id)
    
    if not watches:
        await bot.send(ev, '当前没有监控任何UP主')
        return
    
    up_list = ["📢 当前监控的UP主列表:", "━━━━━━━━━━━━━━━━━━"]
    for up_name, info in watches.items():
        last_check = datetime.fromisoformat(info['last_check']).strftime('%m-%d %H:%M')
        up_list.append(f"👤 {up_name} | 最后检查: {last_check}")
        up_list.append("━━━━━━━━━━━━━━━━━━")
    
    await bot.send(ev, "\n".join(up_list))

@sv.scheduled_job('interval', minutes=UP_WATCH_INTERVAL)
async def check_up_updates():
    all_watches = watch_storage.get_all_watches()
    if not all_watches:
        return
    
    bot = sv.bot
    
    for group_id_str, up_dict in all_watches.items():
        group_id = int(group_id_str)
        for up_name, info in up_dict.items():
            try:
                last_vid = info.get('last_vid')
                
                # 通过名称搜索最新视频
                results = await get_bilibili_search(up_name, "up")
                if not results:
                    continue
                
                # 精确匹配名称
                latest_video = next(
                    (v for v in results 
                     if normalize_name(v['author']) == normalize_name(up_name)),
                    None
                )
                
                if not latest_video:
                    continue
                
                # 检查是否为新视频
                if latest_video['bvid'] != last_vid:
                    # 验证发布时间是否晚于上次检查时间
                    last_check_time = datetime.fromisoformat(info['last_check'])
                    video_pub_time = datetime.fromtimestamp(latest_video['pubdate'])
                    
                    if video_pub_time > last_check_time:
                        pub_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(latest_video['pubdate']))
                        
                        # 处理图片URL
                        pic_url = latest_video['pic']
                        if not pic_url.startswith(('http://', 'https://')):
                            pic_url = 'https:' + pic_url
                        proxied_url = f'https://images.weserv.nl/?url={quote(pic_url.replace("https://", "").replace("http://", ""), safe="")}'
                        
                        msg = [
                            f"📢 UP主【{up_name}】发布了新视频！",
                            f"标题: {latest_video['title']}",
                            f"[CQ:image,file={proxied_url}]",
                            f"发布时间: {pub_time}",
                            f"视频链接: https://b23.tv/{latest_video['bvid']}"
                        ]
                        
                        watch_storage.update_last_video(
                            group_id=group_id,
                            up_name=up_name,
                            last_vid=latest_video['bvid']
                        )
                        
                        await bot.send_group_msg(group_id=group_id, message="\n".join(msg))
                
            except Exception as e:
                sv.logger.error(f'监控UP主【{up_name}】失败: {str(e)}')

@sv.on_prefix('查视频')
async def search_bilibili_video(bot, ev: CQEvent):
    keyword = ev.message.extract_plain_text().strip()
    if not keyword:
        await bot.send(ev, '请输入搜索关键词，例如：查视频 原神')
        return
    
    try:
        msg_id = (await bot.send(ev, "🔍🔍🔍🔍 搜索中..."))['message_id']
        results = await get_bilibili_search(keyword, "video")
        
        if not results:
            await bot.finish(ev, f'未找到"{keyword}"相关视频')
            return

        reply = ["📺📺📺📺 搜索结果（最多5个）：", "━━━━━━━━━━━━━━━━━━"]
        for i, video in enumerate(results, 1):
            clean_title = re.sub(r'<[^>]+>', '', video['title'])
            pub_time = time.strftime("%Y-%m-%d", time.localtime(video['pubdate']))
            
            # 处理图片URL
            pic_url = video['pic']
            if not pic_url.startswith(('http://', 'https://')):
                pic_url = 'https:' + pic_url
            proxied_url = f'https://images.weserv.nl/?url={quote(pic_url.replace("https://", "").replace("http://", ""), safe="")}'
            
            reply.extend([
                f"{i}. {clean_title}",
                f"[CQ:image,file={proxied_url}]",  # 图片放在标题下方
                f"   📅📅📅📅 {pub_time} | 👤👤👤👤 {video['author']}",
                f"   🔗🔗🔗🔗 https://b23.tv/{video['bvid']}",
                "━━━━━━━━━━━━━━━━━━"
            ])
        
        await safe_send(bot, ev, "\n".join(reply))
    except Exception as e:
        await bot.send(ev, f'搜索失败: {str(e)}')

@sv.scheduled_job('interval', minutes=3)
async def clear_cache():
    global search_cache
    expired_keys = [k for k, (_, t) in search_cache.items() 
                   if datetime.now() - t > timedelta(minutes=CACHE_EXPIRE_MINUTES)]
    for k in expired_keys:
        del search_cache[k]
