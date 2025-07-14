import os
import json
import re
import time
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List
from urllib.parse import quote

from hoshino import Service, priv
from hoshino.typing import CQEvent
import aiohttp

# 主服务定义
sv = Service('b站视频搜索', enable_on_default=True, help_='搜索B站视频\n使用方法：\n1. 查视频 [关键词/名称-up] - 搜索B站视频\n2. 视频关注/关注+ [视频链接] - 通过视频链接关注UP主\n3. 取关up [UP主名称] - 取消监控\n4. 查看关注 - 查看当前监控列表')

# 配置项
MAX_RESULTS = 5
UP_WATCH_INTERVAL = 5  # 监控间隔(分钟)
CACHE_EXPIRE_MINUTES = 3
search_cache = {}

# JSON存储文件路径
WATCH_JSON_PATH = Path(__file__).parent / 'data' / 'bili_watch.json'
os.makedirs(WATCH_JSON_PATH.parent, exist_ok=True)

# 辅助函数定义
def normalize_name(name: str) -> str:
    """标准化名称(去前后空格/小写)"""
    return name.strip().lower()

def process_pic_url(pic_url: str) -> str:
    """处理图片URL"""
    if not pic_url:
        return ""
    if not pic_url.startswith(('http://', 'https://')):
        pic_url = 'https:' + pic_url
    return f'https://images.weserv.nl/?url={quote(pic_url.split("//")[-1])}&w=800&h=450'

class UpWatchStorage:
    def __init__(self):
        self._data = {}  # 主数据结构: {group_id: {up_name: {last_check, last_vid}}}
        self.name_index = {}  # 名称小写索引: {up_name_lower: {group_id: up_name}}
        self._load_data()
        sv.logger.info("UP主监控存储初始化完成")
    
    def _load_data(self):
        """加载数据"""
        try:
            if WATCH_JSON_PATH.exists():
                with open(WATCH_JSON_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # 验证并转换数据格式
                    if isinstance(data, dict):
                        self._data = {}
                        for group_id_str, ups in data.items():
                            if not isinstance(ups, dict):
                                continue
                            self._data[group_id_str] = {}
                            for up_name, info in ups.items():
                                if not isinstance(info, dict):
                                    continue
                                self._data[group_id_str][up_name] = {
                                    'last_check': info.get('last_check', datetime.now().isoformat()),
                                    'last_vid': info.get('last_vid')
                                }
                                # 更新名称索引
                                up_name_lower = normalize_name(up_name)
                                if up_name_lower not in self.name_index:
                                    self.name_index[up_name_lower] = {}
                                self.name_index[up_name_lower][group_id_str] = up_name
        except Exception as e:
            sv.logger.error(f"加载监控数据失败: {str(e)}")
            self._data = {}
            self.name_index = {}
    
    def save(self):
        """保存数据到文件"""
        try:
            with open(WATCH_JSON_PATH, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            sv.logger.error(f"保存监控数据失败: {str(e)}")
    
    def add_watch(self, group_id: int, up_name: str, last_vid: str = None):
        """添加监控"""
        group_id = str(group_id)
        if group_id not in self._data:
            self._data[group_id] = {}
        
        self._data[group_id][up_name] = {
            'last_check': datetime.now().isoformat(),
            'last_vid': last_vid
        }
        
        # 更新名称索引
        up_name_lower = normalize_name(up_name)
        if up_name_lower not in self.name_index:
            self.name_index[up_name_lower] = {}
        self.name_index[up_name_lower][group_id] = up_name
        
        self.save()
        sv.logger.info(f"已添加监控: 群{group_id} -> UP主{up_name}")
    
    def remove_watch(self, group_id: int, up_name: str) -> bool:
        """移除监控（仅移除当前群的监控）"""
        group_id = str(group_id)
        up_name_lower = normalize_name(up_name)
        
        # 检查当前群是否监控了该UP主
        if group_id in self._data and up_name in self._data[group_id]:
            # 删除主数据
            del self._data[group_id][up_name]
            if not self._data[group_id]:  # 如果群监控列表为空，删除整个群条目
                del self._data[group_id]
            
            # 更新名称索引
            if up_name_lower in self.name_index and group_id in self.name_index[up_name_lower]:
                del self.name_index[up_name_lower][group_id]
                if not self.name_index[up_name_lower]:  # 如果该UP主没有被任何群监控，删除整个索引
                    del self.name_index[up_name_lower]
            
            self.save()
            sv.logger.info(f"已移除监控: 群{group_id} -> UP主{up_name}")
            return True
        
        sv.logger.warning(f"移除监控失败: 群{group_id} 未监控 UP主{up_name}")
        return False
    
    def get_group_watches(self, group_id: int) -> Dict[str, Dict[str, Any]]:
        """获取群组监控列表"""
        group_id = str(group_id)
        return self._data.get(group_id, {})
    
    def get_all_watches(self) -> Dict[str, Any]:
        """获取所有监控数据"""
        return self._data
    
    def update_last_video(self, group_id: int, up_name: str, last_vid: str):
        """更新最后视频记录"""
        group_id = str(group_id)
        if group_id in self._data and up_name in self._data[group_id]:
            self._data[group_id][up_name].update({
                'last_vid': last_vid,
                'last_check': datetime.now().isoformat()
            })
            self.save()
    
    def find_up_by_name(self, name: str) -> Dict[str, str]:
        """通过名称查找UP主"""
        return self.name_index.get(normalize_name(name), {})

# 全局存储实例
watch_storage = UpWatchStorage()

def normalize_name(name: str) -> str:
    """标准化名称(去前后空格/小写)"""
    return name.strip().lower()

def process_pic_url(pic_url: str) -> str:
    """处理图片URL"""
    if not pic_url:
        return ""
    if not pic_url.startswith(('http://', 'https://')):
        pic_url = 'https:' + pic_url
    return f'https://images.weserv.nl/?url={quote(pic_url.split("//")[-1])}&w=800&h=450'

async def get_video_info(bvid: str) -> Optional[Dict]:
    """获取视频详细信息"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Referer': f'https://www.bilibili.com/video/{bvid}'
    }
    url = f'https://api.bilibili.com/x/web-interface/view?bvid={bvid}'
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    sv.logger.error(f"获取视频信息失败: HTTP {resp.status}")
                    return None
                data = await resp.json()
                if data.get('code') == 0:
                    return data['data']
                sv.logger.error(f"视频API返回错误: {data.get('message')}")
        except Exception as e:
            sv.logger.error(f"获取视频信息异常: {str(e)}")
    return None

async def get_bilibili_search(keyword: str, search_type: str = "video") -> List[Dict]:
    """统一搜索函数"""
    cache_key = f"{search_type}:{normalize_name(keyword)}"
    if cache_key in search_cache:
        cached_data, timestamp = search_cache[cache_key]
        if datetime.now() - timestamp < timedelta(minutes=CACHE_EXPIRE_MINUTES):
            return cached_data[:MAX_RESULTS]

    params = {
        'search_type': 'video',
        'keyword': keyword,
        'order': 'pubdate' if search_type == "up" else 'totalrank',
        'ps': MAX_RESULTS * 2,
        'platform': 'web'
    }

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Referer': 'https://www.bilibili.com/',
        'Cookie': 'buvid3=XXXXXX;'
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
                    raw_results = data['data'].get('result', [])
                    # 精确筛选结果
                    results = []
                    for video in raw_results:
                        if len(results) >= MAX_RESULTS:
                            break
                        # UP主搜索模式需要作者匹配
                        if search_type == "up" and normalize_name(video.get('author', '')) != normalize_name(keyword):
                            continue
                        results.append(video)
                    
                    search_cache[cache_key] = (results, datetime.now())
                    return results
                sv.logger.error(f"API返回错误: {data.get('message')}")
        except Exception as e:
            sv.logger.error(f"搜索失败: {str(e)}")
    return []

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

@sv.on_prefix(('视频关注','关注'))
async def watch_by_video(bot, ev: CQEvent):
    """通过视频链接关注UP主"""
    video_url = ev.message.extract_plain_text().strip()
    if not video_url:
        await bot.send(ev, '请输入视频链接，例如：视频关注 https://www.bilibili.com/video/BV1B73kzcE1e')
        return
    
    # 提取BV号
    bvid = None
    patterns = [
        r'bilibili\.com/video/(BV[0-9A-Za-z]+)',
        r'b23\.tv/(BV[0-9A-Za-z]+)',
        r'(BV[0-9A-Za-z]+)',
        r'bilibili\.com/video/av\d+\?.*bv=(BV[0-9A-Za-z]+)',
        r'video/(BV[0-9A-Za-z]+)/?'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, video_url)
        if match:
            bvid = match.group(1)
            break
    
    if not bvid:
        await bot.send(ev, '⚠️ 无法识别视频BV号，请确认链接格式正确\n'
                         '📌 支持格式示例:\n'
                         '1. https://www.bilibili.com/video/BV1B73kzcE1e\n'
                         '2. https://b23.tv/BV1B73kzcE1e\n'
                         '3. BV1B73kzcE1e')
        return
    
    group_id = ev.group_id
    
    try:
        # 获取视频信息
        video_info = await get_video_info(bvid)
        if not video_info:
            await bot.send(ev, '❌ 获取视频信息失败，请检查BV号是否正确或稍后再试')
            return
        
        up_name = video_info['owner']['name']
        
        # 检查本群是否已关注
        group_watches = watch_storage.get_group_watches(group_id)
        if up_name in group_watches:
            last_check = datetime.fromisoformat(group_watches[up_name]['last_check']).strftime('%m-%d %H:%M')
            await bot.send(ev, f'ℹ️ 本群已关注【{up_name}】\n'
                             f'⏰ 最后检查时间: {last_check}')
            return
        
        # 添加到本群监控
        watch_storage.add_watch(
            group_id=group_id,
            up_name=up_name,
            last_vid=bvid
        )
        
        # 构建响应消息
        pub_time = datetime.fromtimestamp(video_info['pubdate']).strftime('%Y-%m-%d %H:%M')
        pic_url = process_pic_url(video_info['pic'])
        
        msg = [
            f'✅ 成功关注UP主【{up_name}】',
            f'📺 视频标题: {video_info["title"]}',
            f'[CQ:image,file={pic_url}]',
            f'⏰ 发布时间: {pub_time}',
            f'🔗 视频链接: https://b23.tv/{bvid}',
            '📢 该UP主的新视频将会通知本群'
        ]
        
        await bot.send(ev, '\n'.join(msg))
        
    except aiohttp.ClientError as e:
        await bot.send(ev, f'🌐 网络请求失败: {str(e)}\n请稍后再试')
    except json.JSONDecodeError:
        await bot.send(ev, '❌ 数据解析失败，可能是B站API变更\n请通知维护人员检查')
    except Exception as e:
        sv.logger.error(f'视频关注功能异常: {type(e).__name__}: {str(e)}')
        await bot.send(ev, f'⚠️ 发生未知错误: {str(e)}\n请检查日志获取详细信息')

@sv.on_prefix(('取关up','取关'))
async def unwatch_bilibili_up(bot, ev: CQEvent):
    """取关指定UP主（仅当前群）"""
    up_name = ev.message.extract_plain_text().strip()
    group_id = ev.group_id
    
    # 查找准确的UP主名称（不区分大小写）
    found = watch_storage.find_up_by_name(up_name)
    if not found or str(group_id) not in found:
        await bot.send(ev, f'本群未监控【{up_name}】')
        return
    
    # 获取准确的UP主名称（保留大小写）
    exact_name = found[str(group_id)]
    
    if watch_storage.remove_watch(group_id, exact_name):
        await bot.send(ev, f'✅ 已取消对【{exact_name}】的监控')
    else:
        await bot.send(ev, '❌ 取关失败，请稍后再试')

@sv.on_fullmatch('查看关注')
async def list_watched_ups(bot, ev: CQEvent):
    """查看当前群监控的UP主列表"""
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
    """定时检查UP主更新"""
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
                # 添加延迟防止请求过于频繁
                await asyncio.sleep(10)
                
                last_vid = info.get('last_vid')
                sv.logger.info(f"检查UP主【{up_name}】更新，上次记录视频: {last_vid or '无'}")                

                # 通过上次保存的BV号获取UP主mid
                if last_vid:
                    video_info = await get_video_info(last_vid)
                    if not video_info:
                        sv.logger.warning(f"无法获取上次视频信息: {last_vid}")
                        continue
                    
                    up_mid = video_info['owner']['mid']
                    
                    # 获取UP主空间最新视频
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                        'Referer': f'https://space.bilibili.com/{up_mid}'
                    }
                    url = f'https://api.bilibili.com/x/space/arc/search?mid={up_mid}&ps=5&order=pubdate'
                    
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, headers=headers, timeout=10) as resp:
                            if resp.status != 200:
                                sv.logger.error(f"获取UP主空间失败: HTTP {resp.status}")
                                continue
                            data = await resp.json()
                            if data.get('code') != 0:
                                sv.logger.error(f"UP主空间API返回错误: {data.get('message')}")
                                continue
                            
                            vlist = data['data']['list']['vlist']
                            if not vlist:
                                sv.logger.info(f"UP主【{up_name}】空间没有视频")
                                continue
                            
                            latest_video = vlist[0]
                            current_bvid = latest_video['bvid']
                            video_pub_time = datetime.fromtimestamp(latest_video['created'])
                            
                            # 验证是否为新视频
                            is_new = False
                            reason = ""
                            
                            if not last_vid:
                                is_new = True
                                reason = "首次监控该UP主"
                            else:
                                # 检查BV号是否相同
                                if current_bvid == last_vid:
                                    reason = "BV号相同，视频未更新"
                                else:
                                    # 获取上次视频信息
                                    last_video_info = await get_video_info(last_vid)
                                    if not last_video_info:
                                        reason = "无法获取上次视频信息，保守处理不推送"
                                    else:
                                        last_pub_time = datetime.fromtimestamp(last_video_info['pubdate'])
                                        # 比较发布时间（增加2分钟缓冲）
                                        if video_pub_time > (last_pub_time + timedelta(minutes=2)):
                                            is_new = True
                                            reason = (f"新视频发布时间({video_pub_time}) > "
                                                     f"上次视频发布时间({last_pub_time})")
                                        else:
                                            reason = "无新发布(发布时间未超过阈值)"
                            
                            sv.logger.info(f"更新判断: {reason}")
                            
                            # 如果是新视频则更新记录并推送
                            if is_new:
                                watch_storage.update_last_video(
                                    group_id=group_id,
                                    up_name=up_name,
                                    last_vid=current_bvid
                                )
                                
                                # 准备通知内容
                                pub_time = video_pub_time.strftime("%Y-%m-%d %H:%M")
                                pic_url = process_pic_url(latest_video['pic'])
                                
                                msg = [
                                    f"📢📢 UP主【{up_name}】发布了新视频！",
                                    f"📺📺 标题: {latest_video['title']}",
                                    f"[CQ:image,file={pic_url}]",
                                    f"⏰⏰⏰ 发布时间: {pub_time}",
                                    f"🔗🔗 视频链接: https://b23.tv/{current_bvid}"
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
    """搜索B站视频"""
    raw_input = ev.message.extract_plain_text().strip()
    if not raw_input:
        await bot.send(ev, '请输入搜索指令，例如：\n1. 查视频 原神\n2. 查视频 老番茄-up')
        return
    
    # 解析-up参数
    keyword = None
    up_name = None
    
    if '-up' in raw_input:
        parts = re.split(r'\s*-up\s*', raw_input, 1)
        if len(parts) > 0:
            keyword = parts[0].strip() if parts[0].strip() else None
        if len(parts) > 1:
            up_name = parts[1].strip()
        
        # 处理"老番茄-up"情况
        if not up_name and keyword:
            up_name = keyword
            keyword = None
    else:
        keyword = raw_input
    
    try:
        msg_id = (await bot.send(ev, "🔍 搜索中..."))['message_id']
        
        # 获取搜索结果
        if up_name:
            results = await get_bilibili_search(up_name, "up")
            if not results:
                await bot.finish(ev, f'未找到UP主【{up_name}】的视频')
                return
        else:
            search_term = keyword if keyword is not None else raw_input
            results = await get_bilibili_search(search_term)
            if not results:
                await bot.finish(ev, f'未找到"{search_term}"相关视频')
                return
        
        # 构建回复
        reply = ["📺 搜索结果（最多5个）：", "━━━━━━━━━━━━━━━━━━"]
        for i, video in enumerate(results[:MAX_RESULTS], 1):
            clean_title = re.sub(r'<[^>]+>', '', video['title'])
            pub_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(video['pubdate']))
            
            pic_url = process_pic_url(video['pic'])
            
            reply.extend([
                f"{i}. {clean_title}",
                f"[CQ:image,file={pic_url}]",
                f"   📅 {pub_time} | 👤 {video['author']}",
                f"   🔗 https://b23.tv/{video['bvid']}",
                "━━━━━━━━━━━━━━━━━━"
            ])
        
        await safe_send(bot, ev, "\n".join(reply))
        
    except Exception as e:
        await bot.send(ev, f'搜索失败: {str(e)}')
