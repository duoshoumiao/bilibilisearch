import os
import json
import re
import time
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional
from urllib.parse import quote

from hoshino import Service, priv
from hoshino.typing import CQEvent
import aiohttp

# 主服务定义
sv = Service('b站视频搜索', enable_on_default=True, help_='搜索B站视频\n使用方法：\n1. 查视频 [关键词] - 搜索B站视频\n2. 查up [UP主名称] - 搜索UP主视频\n3. 关注up [UP主名称] - 监控UP主新视频\n4. 取关up [UP主名称] - 取消监控\n5. 查看关注 - 查看当前监控列表')

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
        self._data = self._load_data()
    
    def _load_data(self) -> Dict[str, Any]:
        try:
            if WATCH_JSON_PATH.exists():
                with open(WATCH_JSON_PATH, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            sv.logger.error(f"加载监控数据失败: {str(e)}")
        return {}
    
    def save(self):
        try:
            with open(WATCH_JSON_PATH, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            sv.logger.error(f"保存监控数据失败: {str(e)}")
    
    def add_watch(self, group_id: int, up_name: str, up_uid: str, last_vid: str = None):
        group_id = str(group_id)
        if group_id not in self._data:
            self._data[group_id] = {}
        self._data[group_id][up_uid] = {
            'up_name': up_name,
            'last_check': datetime.now().isoformat(),
            'last_vid': last_vid
        }
        self.save()
    
    def remove_watch(self, group_id: int, up_name_or_uid: str) -> Optional[str]:
        group_id = str(group_id)
        if group_id not in self._data:
            return None
            
        # 先尝试按UID查找
        if up_name_or_uid in self._data[group_id]:
            del self._data[group_id][up_name_or_uid]
            if not self._data[group_id]:
                del self._data[group_id]
            self.save()
            return up_name_or_uid
            
        # 按名称查找
        for uid, info in self._data[group_id].items():
            if info['up_name'].lower() == up_name_or_uid.lower():
                del self._data[group_id][uid]
                if not self._data[group_id]:
                    del self._data[group_id]
                self.save()
                return uid
                
        return None
    
    def get_group_watches(self, group_id: int) -> Dict[str, Dict[str, Any]]:
        group_id = str(group_id)
        return self._data.get(group_id, {})
    
    def get_all_watches(self) -> Dict[str, Any]:
        return self._data
    
    def update_last_video(self, group_id: int, up_uid: str, last_vid: str, new_name: str = None):
        group_id = str(group_id)
        if group_id in self._data and up_uid in self._data[group_id]:
            if new_name:
                self._data[group_id][up_uid]['up_name'] = new_name
            self._data[group_id][up_uid].update({
                'last_vid': last_vid,
                'last_check': datetime.now().isoformat()
            })
            self.save()
    
    def get_up_name_by_uid(self, group_id: int, up_uid: str) -> Optional[str]:
        group_id = str(group_id)
        if group_id in self._data and up_uid in self._data[group_id]:
            return self._data[group_id][up_uid].get('up_name')
        return None
    
    def get_up_uid_by_name(self, group_id: int, up_name: str) -> Optional[str]:
        group_id = str(group_id)
        if group_id not in self._data:
            return None
        for uid, info in self._data[group_id].items():
            if info['up_name'].lower() == up_name.lower():
                return uid
        return None

# 全局存储实例
watch_storage = UpWatchStorage()

async def get_bilibili_search(keyword: str, search_type: str = "video"):
    cache_key = f"{search_type}:{keyword.lower()}"
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
                    return None
                data = await resp.json()
                if data.get('code') == 0:
                    results = data['data'].get('result', [])[:MAX_RESULTS]
                    if search_type == "up":
                        # 确保结果中包含mid(UID)字段
                        for r in results:
                            if 'mid' not in r:
                                r['mid'] = r.get('up_id', '')  # 使用备用字段
                        # 如果是UID搜索，直接返回结果
                        if keyword.isdigit():
                            return results
                        # 按UP主名称精确匹配
                        results = [v for v in results if v['author'].lower() == keyword.lower()]
                        # 如果没有精确匹配结果，返回第一个结果（可能是UP主改名前的内容）
                        if not results and data['data'].get('result'):
                            results = [data['data']['result'][0]]
                    search_cache[cache_key] = (results, datetime.now())
                    return results
        except Exception as e:
            sv.logger.error(f"搜索失败: {str(e)}")
        return []

async def safe_send(bot, ev, message):
    """安全发送消息，防止消息体解析错误"""
    try:
        if not message or message.strip() == '':
            return
            
        if isinstance(message, list):
            message = '\n'.join(message)
            
        await bot.send(ev, message)
    except Exception as e:
        sv.logger.error(f'发送消息失败: {str(e)}')
        try:
            # 尝试发送简化版消息
            simple_msg = re.sub(r'\[CQ:image[^\]]+\]', '', message)
            if simple_msg.strip():
                await bot.send(ev, simple_msg)
        except Exception as e2:
            sv.logger.error(f'发送简化消息也失败: {str(e2)}')

@sv.on_prefix('关注up')
async def watch_bilibili_up(bot, ev: CQEvent):
    up_name = ev.message.extract_plain_text().strip()
    if not up_name:
        await bot.send(ev, '请输入UP主名称，例如：关注up 老番茄')
        return
    
    group_id = ev.group_id
    
    # 检查是否已经关注
    existing_uid = watch_storage.get_up_uid_by_name(group_id, up_name)
    if existing_uid:
        current_name = watch_storage.get_up_name_by_uid(group_id, existing_uid)
        await bot.send(ev, f'【{current_name}】(UID:{existing_uid})已经在监控列表中了')
        return
    
    try:
        results = await get_bilibili_search(up_name, "up")
        if not results:
            await bot.send(ev, f'未找到UP主【{up_name}】，请确认名称是否正确')
            return
        
        latest_video = results[0]
        up_uid = str(latest_video['mid'])
        current_name = latest_video['author']
        
        # 再次检查UID是否已存在
        if watch_storage.get_up_name_by_uid(group_id, up_uid):
            current_name = watch_storage.get_up_name_by_uid(group_id, up_uid)
            await bot.send(ev, f'【{current_name}】(UID:{up_uid})已经在监控列表中了')
            return
        
        watch_storage.add_watch(
            group_id=group_id,
            up_name=current_name,
            up_uid=up_uid,
            last_vid=latest_video['bvid']
        )
        
        await bot.send(ev, f'✅ 成功关注UP主【{current_name}】(UID:{up_uid})\n最新视频: {latest_video["title"]}\n将监控后续更新')
    except Exception as e:
        await bot.send(ev, f'关注失败: {str(e)}')

@sv.on_prefix('取关up')
async def unwatch_bilibili_up(bot, ev: CQEvent):
    up_name_or_uid = ev.message.extract_plain_text().strip()
    group_id = ev.group_id
    
    removed_uid = watch_storage.remove_watch(group_id, up_name_or_uid)
    if not removed_uid:
        await bot.send(ev, f'未找到【{up_name_or_uid}】的监控记录')
        return
    
    await bot.send(ev, f'✅ 已取消对UID【{removed_uid}】的监控')

@sv.on_fullmatch('查看关注')
async def list_watched_ups(bot, ev: CQEvent):
    group_id = ev.group_id
    watches = watch_storage.get_group_watches(group_id)
    
    if not watches:
        await bot.send(ev, '当前没有监控任何UP主')
        return
    
    up_list = ["📢📢📢📢 当前监控的UP主列表:", "━━━━━━━━━━━━━━━━━━"]
    for up_uid, info in watches.items():
        last_check = datetime.fromisoformat(info['last_check']).strftime('%m-%d %H:%M')
        up_list.append(f"👤👤👤👤 {info['up_name']} (UID:{up_uid}) | 最后检查: {last_check}")
        up_list.append("━━━━━━━━━━━━━━━━━━")
    
    await bot.send(ev, "\n".join(up_list))

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

@sv.on_prefix('查up')
async def search_bilibili_up(bot, ev: CQEvent):
    up_name = ev.message.extract_plain_text().strip()
    if not up_name:
        await bot.send(ev, '请输入UP主名称，例如：查up 老番茄')
        return

    try:
        msg_id = (await bot.send(ev, f"🔍🔍🔍🔍 正在搜索【{up_name}】的最新视频..."))['message_id']
        
        results = await get_bilibili_search(up_name, "up")
        if not results:
            await bot.finish(ev, f'未找到UP主【{up_name}】的视频')
            return

        reply = [f"👤👤👤👤 {results[0]['author']} (UID:{results[0]['mid']}) 的搜索结果（最多5个）：", "━━━━━━━━━━━━━━━━━━"]
        for i, video in enumerate(results, 1):
            pub_time = time.strftime("%Y-%m-%d", time.localtime(video['pubdate']))
            
            # 处理图片URL
            pic_url = video['pic']
            if not pic_url.startswith(('http://', 'https://')):
                pic_url = 'https:' + pic_url
            proxied_url = f'https://images.weserv.nl/?url={quote(pic_url.replace("https://", "").replace("http://", ""), safe="")}'
            
            reply.extend([
                f"{i}. {re.sub(r'<[^>]+>', '', video['title'])}",
                f"[CQ:image,file={proxied_url}]",  # 图片放在标题下方
                f"   📅📅📅📅 {pub_time} | 👀👀👀👀 {video.get('play', 0)}播放",
                f"   🔗🔗🔗🔗 https://b23.tv/{video['bvid']}",
                "━━━━━━━━━━━━━━━━━━"
            ])
        await safe_send(bot, ev, "\n".join(reply))

    except Exception as e:
        await bot.send(ev, f'搜索失败: {str(e)}')

@sv.scheduled_job('interval', minutes=UP_WATCH_INTERVAL)
async def check_up_updates():
    all_watches = watch_storage.get_all_watches()
    if not all_watches:
        return
    
    bot = sv.bot
    
    for group_id_str, up_dict in all_watches.items():
        group_id = int(group_id_str)
        for up_uid, info in up_dict.items():
            try:
                current_up_name = info['up_name']
                # 优先使用UID搜索UP主最新视频
                results = await get_bilibili_search(up_uid, "up")
                if not results:
                    # 如果使用UID搜索不到，再尝试用名称搜索
                    results = await get_bilibili_search(current_up_name, "up")
                    if not results:
                        continue
                
                # 确保找到的视频确实是该UP主的（UID匹配）
                latest_video = None
                for video in results:
                    if str(video.get('mid')) == up_uid:
                        latest_video = video
                        break
                
                if not latest_video:
                    continue
                
                current_time = datetime.now()
                
                # 检查UP主是否改名（只有在UID匹配的情况下才比较名称）
                new_name = latest_video['author']
                name_changed = new_name.lower() != current_up_name.lower()
                
                if latest_video['bvid'] != info.get('last_vid') or name_changed:
                    pub_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(latest_video['pubdate']))
                    
                    # 处理图片URL
                    pic_url = latest_video['pic']
                    if not pic_url.startswith(('http://', 'https://')):
                        pic_url = 'https:' + pic_url
                    proxied_url = f'https://images.weserv.nl/?url={quote(pic_url.replace("https://", "").replace("http://", ""), safe="")}'
                    
                    msg = [
                        f"📢📢📢📢 UP主【{new_name if name_changed else current_up_name}】(UID:{up_uid})发布了新视频！",
                        f"标题: {latest_video['title']}",
                        f"[CQ:image,file={proxied_url}]",  # 图片放在标题下方
                        f"发布时间: {pub_time}",
                        f"视频链接: https://b23.tv/{latest_video['bvid']}",
                        "━━━━━━━━━━━━━━━━━━"
                    ]
                    
                    watch_storage.update_last_video(
                        group_id=group_id,
                        up_uid=up_uid,
                        last_vid=latest_video['bvid'],
                        new_name=new_name if name_changed else None
                    )
                    
                    if name_changed:
                        msg.insert(1, f"⚠️ 注意：UP主已从【{current_up_name}】改名为【{new_name}】")
                    
                    await bot.send_group_msg(group_id=group_id, message="\n".join(msg))
                
            except Exception as e:
                sv.logger.error(f'监控UP主UID【{up_uid}】失败: {str(e)}')
                continue

@sv.scheduled_job('interval', minutes=3)
async def clear_cache():
    global search_cache
    expired_keys = [k for k, (_, t) in search_cache.items() 
                   if datetime.now() - t > timedelta(minutes=CACHE_EXPIRE_MINUTES)]
    for k in expired_keys:
        del search_cache[k]
