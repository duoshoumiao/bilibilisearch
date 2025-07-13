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

from hoshino import Service, priv
from hoshino.typing import CQEvent
import aiohttp

# 主服务定义
sv = Service('b站视频搜索', enable_on_default=True, help_='搜索B站视频\n使用方法：\n1. 查视频 [关键词] - 搜索B站视频\n2. 查up [UP主名称] - 搜索UP主视频\n3. 关注up [UP主名称] - 监控UP主新视频\n4. 视频关注 [视频链接] - 通过视频链接关注UP主\n5. 取关up [UP主名称/UID] - 取消监控\n6. 查看关注 - 查看当前监控列表')

# 配置项
MAX_RESULTS = 5
UP_WATCH_INTERVAL = 10
CACHE_EXPIRE_MINUTES = 3
NAME_SIMILARITY_THRESHOLD = 0.6  # 名称相似度阈值
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

def is_name_similar(old_name: str, new_name: str, threshold=NAME_SIMILARITY_THRESHOLD) -> bool:
    """检查两个名称是否相似，防止账号转让等情况"""
    old_name = old_name.lower().strip()
    new_name = new_name.lower().strip()
    
    # 完全匹配
    if old_name == new_name:
        return True
    
    # 相似度检查
    similarity = difflib.SequenceMatcher(None, old_name, new_name).ratio()
    return similarity >= threshold

async def get_bilibili_search(keyword: str, search_type: str = "video") -> list:
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
                    return []
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
    
    # 严格名称搜索函数
    async def strict_search(name):
        results = await get_bilibili_search(name, "up")
        if not results:
            return None
        
        # 精确匹配名称（包括大小写和空格）
        exact_matches = [
            v for v in results 
            if v['author'].strip().lower() == name.strip().lower()
        ]
        
        # 优先返回精确匹配结果
        return exact_matches[0] if exact_matches else results[0]

    try:
        # 1. 执行严格搜索
        video_data = await strict_search(up_name)
        if not video_data:
            await bot.send(ev, f'未找到UP主【{up_name}】，请确认名称是否正确')
            return
        
        found_uid = str(video_data['mid'])
        found_name = video_data['author']
        
        # 2. 验证名称匹配度
        if found_name.lower() != up_name.lower():
            await bot.send(ev, f'⚠️ 最接近的结果是【{found_name}】(UID:{found_uid})\n'
                              f'与您输入的【{up_name}】不完全匹配\n'
                              '如果确认要关注，请再次发送指令或者改用关注UID 123456789')
            return
        
        # 3. 检查是否已关注
        if watch_storage.get_up_name_by_uid(group_id, found_uid):
            await bot.send(ev, f'【{found_name}】(UID:{found_uid})已在监控列表中')
            return
        
        # 4. 添加到监控
        watch_storage.add_watch(
            group_id=group_id,
            up_name=found_name,
            up_uid=found_uid,
            last_vid=video_data['bvid']
        )
        
        await bot.send(ev, f'✅ 已严格匹配关注UP主【{found_name}】(UID:{found_uid})\n'
                          f'最新视频: {video_data["title"]}\n'
                          '将监控后续更新')
        
    except Exception as e:
        await bot.send(ev, f'关注失败: {str(e)}')

@sv.on_prefix('视频关注')
async def watch_by_video(bot, ev: CQEvent):
    """通过视频链接关注UP主"""
    video_url = ev.message.extract_plain_text().strip()
    if not video_url:
        await bot.send(ev, '请输入视频链接，例如：通过视频关注 https://www.bilibili.com/video/BV1xxx')
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
        
        up_uid = str(video_info['owner']['mid'])
        up_name = video_info['owner']['name']
        
        # 检查是否已关注
        if watch_storage.get_up_name_by_uid(group_id, up_uid):
            await bot.send(ev, f'【{up_name}】(UID:{up_uid})已在监控列表中')
            return
        
        # 添加到监控
        watch_storage.add_watch(
            group_id=group_id,
            up_name=up_name,
            up_uid=up_uid,
            last_vid=bvid
        )
        
        await bot.send(ev, f'✅ 已通过视频关注UP主【{up_name}】(UID:{up_uid})\n'
                         f'视频标题: {video_info["title"]}\n'
                         '将监控后续更新')
        
    except Exception as e:
        await bot.send(ev, f'通过视频关注失败: {str(e)}')

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
                last_vid = info.get('last_vid')
                
                # 方法1：使用UID直接获取UP主空间最新视频（最准确）
                space_url = f"https://api.bilibili.com/x/space/arc/search?mid={up_uid}&ps=1&order=pubdate"
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
                }
                
                latest_video = None
                async with aiohttp.ClientSession() as session:
                    # 尝试通过UID获取空间视频
                    async with session.get(space_url, headers=headers, timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get('code') == 0 and data['data'].get('list', {}).get('vlist'):
                                video = data['data']['list']['vlist'][0]
                                latest_video = {
                                    'bvid': video['bvid'],
                                    'title': video['title'],
                                    'author': current_up_name,  # 空间API不返回作者名，使用存储的名称
                                    'mid': int(up_uid),
                                    'pic': video['pic'],
                                    'pubdate': video['created']
                                }
                
                # 方法2：如果空间API失败，使用搜索API通过UID搜索（确保找到的是该UP主的视频）
                if not latest_video:
                    results = await get_bilibili_search(up_uid, "up")
                    if results:
                        for video in results:
                            if str(video.get('mid')) == up_uid:
                                latest_video = video
                                break
                
                # 方法3：如果仍然找不到，尝试用存储的名称搜索（处理改名情况）
                if not latest_video:
                    results = await get_bilibili_search(current_up_name, "up")
                    if results:
                        for video in results:
                            if str(video.get('mid')) == up_uid:
                                latest_video = video
                                break
                
                # 最终验证
                if not latest_video:
                    sv.logger.warning(f'无法确认UP主UID【{up_uid}】是否有更新，可能已删除账号或更改隐私设置')
                    continue
                
                # 严格验证UID匹配
                if str(latest_video.get('mid')) != up_uid:
                    sv.logger.warning(f'UP主UID不匹配！监控UID:{up_uid}，视频UID:{latest_video.get("mid")}')
                    continue
                
                # 名称相似度检查
                new_name = latest_video['author']
                if not is_name_similar(current_up_name, new_name):
                    sv.logger.warning(f'UP主名称变化过大！原名称:{current_up_name}，新名称:{new_name}')
                    # 发送警告通知
                    await bot.send_group_msg(
                        group_id=group_id,
                        message=f"⚠️ UP主UID【{up_uid}】名称变化过大！\n"
                               f"原名称: {current_up_name}\n"
                               f"新名称: {new_name}\n"
                               "可能是账号已转让，建议手动确认并重新关注"
                    )
                    continue
                
                current_time = datetime.now()
                name_changed = new_name.lower() != current_up_name.lower()
                
                # 严格检查视频是否为新发布的
                if latest_video['bvid'] != last_vid:
                    # 额外验证发布时间是否晚于上次检查时间（防止获取到旧视频）
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
                            f"📢 UP主【{new_name if name_changed else current_up_name}】(UID:{up_uid})发布了新视频！",
                            f"标题: {latest_video['title']}",
                            f"[CQ:image,file={proxied_url}]",
                            f"发布时间: {pub_time}",
                            f"视频链接: https://b23.tv/{latest_video['bvid']}",
                            "━━━━━━━━━━━━━━━━━━"
                        ]
                        
                        if name_changed:
                            msg.insert(1, f"⚠️ 注意：UP主已从【{current_up_name}】改名为【{new_name}】")
                        
                        watch_storage.update_last_video(
                            group_id=group_id,
                            up_uid=up_uid,
                            last_vid=latest_video['bvid'],
                            new_name=new_name if name_changed else None
                        )
                        
                        await bot.send_group_msg(group_id=group_id, message="\n".join(msg))
                    else:
                        # 虽然视频ID不同，但发布时间早于上次检查，可能是数据问题，不提醒
                        sv.logger.info(f'检测到UP主UID【{up_uid}】的视频ID变化但发布时间无更新，可能为数据异常')
                
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
