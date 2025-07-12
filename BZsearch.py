import os
import json
import re
import time
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any

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
    
    def add_watch(self, group_id: int, up_name: str, last_vid: str = None):
        group_id = str(group_id)
        if group_id not in self._data:
            self._data[group_id] = {}
        self._data[group_id][up_name] = {
            'last_check': datetime.now().isoformat(),
            'last_vid': last_vid
        }
        self.save()
    
    def remove_watch(self, group_id: int, up_name: str):
        group_id = str(group_id)
        if group_id in self._data and up_name in self._data[group_id]:
            del self._data[group_id][up_name]
            if not self._data[group_id]:
                del self._data[group_id]
            self.save()
    
    def get_group_watches(self, group_id: int) -> Dict[str, Any]:
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
                        results = [v for v in results if v['author'].lower() == keyword.lower()]
                    search_cache[cache_key] = (results, datetime.now())
                    return results
        except Exception as e:
            sv.logger.error(f"搜索失败: {str(e)}")
        return []

@sv.on_prefix('关注up')
async def watch_bilibili_up(bot, ev: CQEvent):
    up_name = ev.message.extract_plain_text().strip()
    if not up_name:
        await bot.send(ev, '请输入UP主名称，例如：关注up 老番茄')
        return
    
    group_id = ev.group_id
    
    watches = watch_storage.get_group_watches(group_id)
    for watched_up in watches:
        if watched_up.lower() == up_name.lower():
            await bot.send(ev, f'【{watched_up}】已经在监控列表中了')
            return
    
    try:
        results = await get_bilibili_search(up_name, "up")
        if not results:
            await bot.send(ev, f'未找到UP主【{up_name}】，请确认名称是否正确')
            return
        
        latest_video = results[0]
        watch_storage.add_watch(
            group_id=group_id,
            up_name=up_name,
            last_vid=latest_video['bvid']
        )
        
        await bot.send(ev, f'✅ 成功关注UP主【{up_name}】\n最新视频: {latest_video["title"]}\n将监控后续更新')
    except Exception as e:
        await bot.send(ev, f'关注失败: {str(e)}')

@sv.on_prefix('取关up')
async def unwatch_bilibili_up(bot, ev: CQEvent):
    up_name = ev.message.extract_plain_text().strip()
    group_id = ev.group_id
    
    if up_name not in watch_storage.get_group_watches(group_id):
        await bot.send(ev, f'未找到【{up_name}】的监控记录')
        return
    
    watch_storage.remove_watch(group_id, up_name)
    await bot.send(ev, f'✅ 已取消对【{up_name}】的监控')

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
            reply.extend([
                f"{i}. {clean_title}",
                f"   📅 {pub_time} | 👤 {video['author']}",
                f"   🔗 https://b23.tv/{video['bvid']}",
                "━━━━━━━━━━━━━━━━━━"
            ])
        
        await bot.send(ev, "\n".join(reply))
    except Exception as e:
        await bot.send(ev, f'搜索失败: {str(e)}')

@sv.on_prefix('查up')
async def search_bilibili_up(bot, ev: CQEvent):
    up_name = ev.message.extract_plain_text().strip()
    if not up_name:
        await bot.send(ev, '请输入UP主名称，例如：查up 老番茄')
        return

    try:
        msg_id = (await bot.send(ev, f"🔍 正在搜索【{up_name}】的最新视频..."))['message_id']
        
        results = await get_bilibili_search(up_name, "up")
        if not results:
            await bot.finish(ev, f'未找到UP主【{up_name}】的视频')
            return

        reply = [f"👤 {results[0]['author']} 的搜索结果（最多5个）：", "━━━━━━━━━━━━━━━━━━"]
        for i, video in enumerate(results, 1):
            pub_time = time.strftime("%Y-%m-%d", time.localtime(video['pubdate']))
            reply.extend([
                f"{i}. {re.sub(r'<[^>]+>', '', video['title'])}",
                f"   📅 {pub_time} | 👀 {video.get('play', 0)}播放",
                f"   🔗 https://b23.tv/{video['bvid']}",
                "━━━━━━━━━━━━━━━━━━"
            ])
        await bot.send(ev, "\n".join(reply))

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
        for up_name, info in up_dict.items():
            try:
                results = await get_bilibili_search(up_name, "up")
                if not results:
                    continue
                
                latest_video = results[0]
                current_time = datetime.now()
                
                if latest_video['bvid'] != info.get('last_vid'):
                    pub_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(latest_video['pubdate']))
                    msg = [
                        f"📢 UP主【{up_name}】发布了新视频！",
                        f"标题: {latest_video['title']}",
                        f"发布时间: {pub_time}",
                        f"视频链接: https://b23.tv/{latest_video['bvid']}",
                        "━━━━━━━━━━━━━━━━━━"
                    ]
                    
                    watch_storage.update_last_video(
                        group_id=group_id,
                        up_name=up_name,
                        last_vid=latest_video['bvid']
                    )
                    
                    await bot.send_group_msg(group_id=group_id, message="\n".join(msg))
                
            except Exception as e:
                sv.logger.error(f'监控UP主【{up_name}】失败: {str(e)}')
                continue

@sv.scheduled_job('interval', minutes=3)
async def clear_cache():
    global search_cache
    expired_keys = [k for k, (_, t) in search_cache.items() 
                   if datetime.now() - t > timedelta(minutes=CACHE_EXPIRE_MINUTES)]
    for k in expired_keys:
        del search_cache[k]
