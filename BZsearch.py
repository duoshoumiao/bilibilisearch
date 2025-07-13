import os
import json
import re
import time
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from urllib.parse import quote

from hoshino import Service, priv
from hoshino.typing import CQEvent
import aiohttp

# 主服务定义
sv = Service('b站视频搜索', enable_on_default=True, help_='搜索B站视频\n使用方法：\n1. 查视频 [关键词] - 搜索B站视频\n2. 查up [UP主名称] - 搜索UP主视频\n3. 关注up [UP主名称] - 监控UP主新视频\n4. 视频关注 [视频链接] - 通过视频链接关注UP主\n5. 取关up [UP主名称] - 取消监控\n6. 查看关注 - 查看当前监控列表')

# 配置项
MAX_RESULTS = 5
UP_WATCH_INTERVAL = 1  # 监控间隔(分钟)
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
        sv.logger.info("UP主监控存储初始化完成")
    
    def _load_and_migrate(self):
        """加载并自动迁移旧格式数据"""
        try:
            if WATCH_JSON_PATH.exists():
                with open(WATCH_JSON_PATH, 'r', encoding='utf-8') as f:
                    old_data = json.load(f)
                    sv.logger.info(f"从文件加载监控数据，共 {sum(len(v) for v in old_data.values())} 条记录")
                    
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
                        sv.logger.info("数据加载完成，无需迁移")
                        
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
            sv.logger.info("监控数据保存成功")
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
        sv.logger.info(f"已添加监控: 群{group_id} -> UP主{up_name}")
    
    def remove_watch(self, group_id: int, up_name: str) -> bool:
        group_id = str(group_id)
        if group_id in self._data and up_name in self._data[group_id]:
            del self._data[group_id][up_name]
            if up_name.lower() in self.name_index:
                del self.name_index[up_name.lower()]
            if not self._data[group_id]:
                del self._data[group_id]
            self.save()
            sv.logger.info(f"已移除监控: 群{group_id} -> UP主{up_name}")
            return True
        sv.logger.warning(f"移除监控失败: 群{group_id} 未监控 UP主{up_name}")
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
            sv.logger.info(f"更新视频记录: 群{group_id} -> UP主{up_name} -> BV{last_vid}")
    
    def find_up_by_name(self, name: str) -> Optional[Tuple[str, str]]:
        """通过名称查找(不区分大小写)"""
        return self.name_index.get(normalize_name(name))

# 全局存储实例
watch_storage = UpWatchStorage()

def normalize_name(name: str) -> str:
    """标准化名称(去前后空格/小写)"""
    return name.strip().lower()

async def get_up_info_by_name(name: str) -> Optional[Dict]:
    """通过名称获取UP主信息(搜索API)"""
    url = "https://api.bilibili.com/x/web-interface/search/type"
    params = {
        'search_type': 'bili_user',
        'keyword': name,
        'page_size': 1
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Referer': 'https://www.bilibili.com/',
        'Origin': 'https://www.bilibili.com',
        'Accept': 'application/json, text/plain, */*',
        'Cookie': 'buvid3=XXXXXX;'  # 添加必要的cookie
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('code') == 0 and data['data'].get('result'):
                        for user in data['data']['result']:
                            if normalize_name(user['uname']) == normalize_name(name):
                                return {
                                    'mid': str(user['mid']),
                                    'name': user['uname']
                                }
        except Exception as e:
            sv.logger.error(f"获取UP主信息失败: {str(e)}")
    return None

async def get_up_videos(mid: str) -> list:
    """获取UP主空间视频列表"""
    url = "https://api.bilibili.com/x/space/wbi/arc/search"
    params = {
        'mid': mid,
        'ps': MAX_RESULTS,
        'order': 'pubdate'
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Referer': f'https://space.bilibili.com/{mid}/',
        'Origin': 'https://space.bilibili.com',
        'Cookie': 'buvid3=XXXXXX;'  # 添加必要的cookie
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('code') == 0:
                        return [{
                            'bvid': v['bvid'],
                            'title': v['title'],
                            'author': data['data']['list']['name'],
                            'pubdate': v['created'],
                            'pic': v['pic']
                        } for v in data['data']['list']['vlist']]
        except Exception as e:
            sv.logger.error(f"获取UP主视频失败: {str(e)}")
    return []

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
        'Referer': 'https://www.bilibili.com/',
        'Origin': 'https://www.bilibili.com',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Cache-Control': 'no-cache',
        'Cookie': 'buvid3=XXXXXX;'  # 添加必要的cookie
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
                    sv.logger.error(f"搜索请求失败: HTTP {resp.status}")
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
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Referer': f'https://www.bilibili.com/video/{bvid}'
    }
    url = f'https://api.bilibili.com/x/web-interface/view?bvid={bvid}'
    
    async with aiohttp.ClientSession() as session:
        try:
            sv.logger.info(f"获取视频信息: BV{bvid}")
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    sv.logger.error(f"获取视频信息失败: HTTP {resp.status}")
                    return None
                data = await resp.json()
                if data.get('code') == 0:
                    sv.logger.info(f"成功获取视频信息: {data['data']['title']}")
                    return data['data']
                else:
                    sv.logger.error(f"视频API返回错误: {data.get('message')}")
        except Exception as e:
            sv.logger.error(f"获取视频信息异常: {str(e)}")
    return None

async def safe_send(bot, ev, message):
    """安全发送消息"""
    try:
        if not message:
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
    
    # 获取UP主信息
    up_info = await get_up_info_by_name(up_name)
    if not up_info:
        await bot.send(ev, f'未找到UP主【{up_name}】，请确认名称是否正确')
        return
    
    # 获取UP主最新视频
    videos = await get_up_videos(up_info['mid'])
    if not videos:
        await bot.send(ev, f'UP主【{up_name}】没有发布过视频')
        return
    
    latest_video = videos[0]
    
    # 添加到监控
    watch_storage.add_watch(
        group_id=group_id,
        up_name=up_info['name'],
        last_vid=latest_video['bvid']
    )
    
    await bot.send(ev, f'✅ 已关注UP主【{up_info["name"]}】\n'
                      f'最新视频: {latest_video["title"]}\n'
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
    sv.logger.info("开始执行UP主监控检查...")
    all_watches = watch_storage.get_all_watches()
    if not all_watches:
        sv.logger.info("当前没有监控任何UP主")
        return
    
    bot = sv.bot
    update_count = 0
    
    for group_id_str, up_dict in all_watches.items():
        group_id = int(group_id_str)
        for up_name, info in up_dict.items():
            try:
                last_vid = info.get('last_vid')
                last_check_time = datetime.fromisoformat(info['last_check'])
                sv.logger.info(f"检查UP主【{up_name}】更新，上次记录视频: {last_vid}")
                
                # 获取最新视频
                results = await get_bilibili_search(up_name, "up")
                if not results:
                    sv.logger.warning(f"搜索API未找到【{up_name}】的视频，尝试空间API")
                    up_info = await get_up_info_by_name(up_name)
                    if not up_info:
                        continue
                    videos = await get_up_videos(up_info['mid'])
                    if not videos:
                        continue
                    latest_video = videos[0]
                    video_pub_time = datetime.fromtimestamp(latest_video['pubdate'])
                else:
                    # 确保找到的视频确实是该UP主的
                    latest_video = None
                    for video in results:
                        if normalize_name(video['author']) == normalize_name(up_name):
                            latest_video = video
                            break
                    if not latest_video:
                        continue
                    video_pub_time = datetime.fromtimestamp(latest_video['pubdate'])
                
                # 检查是否为新视频
                is_new = False
                if not last_vid:
                    is_new = True
                    reason = "首次监控"
                else:
                    last_video_info = await get_video_info(last_vid)
                    if not last_video_info:
                        is_new = True
                        reason = "无法获取上次视频信息"
                    else:
                        last_pub_time = datetime.fromtimestamp(last_video_info['pubdate'])
                        is_new = video_pub_time > last_pub_time
                        reason = f"新发布时间({video_pub_time}) > 上次发布时间({last_pub_time})" if is_new else "无新发布"
                
                sv.logger.info(f"更新判断: {reason}")
                
                # 无论是否有更新，都更新最后检查时间和视频ID
                watch_storage.update_last_video(
                    group_id=group_id,
                    up_name=up_name,
                    last_vid=latest_video['bvid']  # 总是使用最新找到的视频ID
                )
                
                if is_new:
                    # 准备并发送通知
                    pub_time = video_pub_time.strftime("%Y-%m-%d %H:%M")
                    pic_url = latest_video['pic']
                    if not pic_url.startswith(('http://', 'https://')):
                        pic_url = 'https:' + pic_url
                    proxied_url = f'https://images.weserv.nl/?url={quote(pic_url.replace("https://", "").replace("http://", ""), safe="")}'
                    
                    msg = [
                        f"📢 UP主【{up_name}】发布了新视频！",
                        f"📺 标题: {latest_video['title']}",
                        f"[CQ:image,file={proxied_url}]",
                        f"⏰ 发布时间: {pub_time}",
                        f"🔗 视频链接: https://b23.tv/{latest_video['bvid']}"
                    ]
                    
                    await bot.send_group_msg(group_id=group_id, message="\n".join(msg))
                    update_count += 1
                    sv.logger.info(f"已发送新视频通知: {up_name} - {latest_video['title']}")
                
            except Exception as e:
                sv.logger.error(f'监控UP主【{up_name}】失败: {str(e)}')
                continue
    
    sv.logger.info(f"监控检查完成，共检查 {sum(len(v) for v in all_watches.values())} 个UP主，发现 {update_count} 个更新")
    
@sv.on_prefix('查视频')
async def search_bilibili_video(bot, ev: CQEvent):
    keyword = ev.message.extract_plain_text().strip()
    if not keyword:
        await bot.send(ev, '请输入搜索关键词，例如：查视频 原神')
        return
    
    try:
        msg_id = (await bot.send(ev, "🔍 搜索中..."))['message_id']
        results = await get_bilibili_search(keyword, "video")
        
        if not results:
            await bot.finish(ev, f'未找到"{keyword}"相关视频')
            return

        reply = ["📺 搜索结果（最多5个）：", "━━━━━━━━━━━━━━━━━━"]
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
                f"[CQ:image,file={proxied_url}]",
                f"   📅 {pub_time} | 👤 {video['author']}",
                f"   🔗 https://b23.tv/{video['bvid']}",
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
        msg_id = (await bot.send(ev, f"🔍🔍 正在搜索【{up_name}】的最新视频..."))['message_id']
        
        results = await get_bilibili_search(up_name, "up")
        if not results:
            await bot.finish(ev, f'未找到UP主【{up_name}】的视频')
            return

        reply = [f"👤👤 {results[0]['author']} (UID:{results[0]['mid']}) 的搜索结果（最多5个）：", "━━━━━━━━━━━━━━━━━━"]
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
                f"   📅📅 {pub_time} | 👀👀 {video.get('play', 0)}播放",
                f"   🔗🔗 https://b23.tv/{video['bvid']}",
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
    sv.logger.info(f"清理缓存，移除 {len(expired_keys)} 条过期记录")
