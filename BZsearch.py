from hoshino import Service, priv
from hoshino.typing import CQEvent
import aiohttp
import json
import time
import re
import asyncio
from datetime import datetime, timedelta

sv = Service('b站视频搜索', enable_on_default=True, help_='搜索B站视频\n使用方法：\n1. 查视频 [关键词] - 搜索B站视频（显示5个）\n2. 查up [UP主名称] - 搜索UP主视频（显示5个）')

# 配置项
MAX_RESULTS = 5  # 限制显示结果数量
CACHE_EXPIRE_MINUTES = 3
search_cache = {}

async def get_bilibili_search(keyword: str, search_type: str = "video"):
    """通用搜索函数（自动限制返回5条结果）"""
    cache_key = f"{search_type}:{keyword.lower()}"
    if cache_key in search_cache:
        cached_data, timestamp = search_cache[cache_key]
        if datetime.now() - timestamp < timedelta(minutes=CACHE_EXPIRE_MINUTES):
            return cached_data[:MAX_RESULTS]  # 缓存也限制数量

    params = {
        'search_type': 'video',
        'keyword': keyword,
        'order': 'pubdate' if search_type == "up" else 'totalrank',
        'ps': MAX_RESULTS,  # 直接限制API请求数量
        'platform': 'web'
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Cookie': 'buvid3=XXXXXX'  # 需替换为有效Cookie
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
                    results = data['data'].get('result', [])[:MAX_RESULTS]  # 二次截断
                    if search_type == "up":
                        results = [v for v in results if v['author'].lower() == keyword.lower()]
                    search_cache[cache_key] = (results, datetime.now())
                    return results
        except Exception as e:
            sv.logger.error(f"搜索失败: {str(e)}")
        return []

async def get_up_mid(up_name: str):
    """获取UP主mid（精确匹配）"""
    url = 'https://api.bilibili.com/x/web-interface/search/type'
    params = {
        'search_type': 'bili_user',
        'keyword': up_name,
        'order': 'fans',
        'platform': 'web'
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Referer': 'https://www.bilibili.com/'
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params, headers=headers, timeout=8) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if data.get('code') == 0:
                    for user in data['data']['result']:
                        if user['uname'] == up_name:  # 精确匹配
                            return user['mid']
        except Exception as e:
            sv.logger.error(f"[MID获取] 失败: {str(e)}")
        return None

async def get_up_videos(mid: int):
    """通过mid获取UP主最新5个视频"""
    url = 'https://api.bilibili.com/x/space/wbi/arc/search'
    params = {
        'mid': mid,
        'ps': MAX_RESULTS,  # 限制请求数量
        'pn': 1,
        'order': 'pubdate',
        'platform': 'web'
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Referer': f'https://space.bilibili.com/{mid}/'
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params, headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if data.get('code') == 0:
                    return data['data']['list']['vlist'][:MAX_RESULTS]
        except Exception as e:
            sv.logger.error(f"[视频列表] 获取失败: {str(e)}")
        return []

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
        
        # 优先通过mid获取
        mid = await get_up_mid(up_name)
        if mid:
            videos = await get_up_videos(mid)
            if videos:
                reply = [f"👤 {up_name} 的最新视频（最多5个）：", "━━━━━━━━━━━━━━━━━━"]
                for i, video in enumerate(videos, 1):
                    pub_time = time.strftime("%Y-%m-%d", time.localtime(video['created']))
                    reply.extend([
                        f"{i}. {re.sub(r'<[^>]+>', '', video['title'])}",
                        f"   📅 {pub_time} | 👀 {video.get('play', 0)}播放",
                        f"   🔗 https://b23.tv/{video['bvid']}",
                        "━━━━━━━━━━━━━━━━━━"
                    ])
                await bot.send(ev, "\n".join(reply))
                return

        # 降级使用搜索API
        results = await get_bilibili_search(up_name, "up")
        if not results:
            await bot.finish(ev, f'未找到UP主【{up_name}】的视频')
            return

        reply = [f"👤 {up_name} 的搜索结果（最多5个）：", "━━━━━━━━━━━━━━━━━━"]
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

@sv.scheduled_job('interval', minutes=3)
async def clear_cache():
    global search_cache
    expired_keys = [k for k, (_, t) in search_cache.items() 
                   if datetime.now() - t > timedelta(minutes=CACHE_EXPIRE_MINUTES)]
    for k in expired_keys:
        del search_cache[k]