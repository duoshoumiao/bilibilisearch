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
sv = Service('b站视频搜索', enable_on_default=False, help_='搜索B站视频\n使用方法：\n1. 查视频 [关键词/名称-up] - 搜索B站视频\n2. 视频关注/关注+ [视频链接] - 通过视频链接关注UP主\n3. 取关up [UP主名称] - 取消监控\n4. 查看关注 - 查看当前监控列表')  
  
# 配置项  
MAX_RESULTS = 5  
UP_WATCH_INTERVAL = 30  # 监控间隔(分钟)  
CACHE_EXPIRE_MINUTES = 3  
UP_CHECK_DELAY = 5      # 每个UP主检查之间的延迟(秒)  
RATE_LIMIT_BACKOFF = 60 # 触发频控时的退避时间(秒)  
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
        self._data = {}  # 主数据结构: {group_id: {up_name: {last_check, last_vid, mid}}}  
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
                                    'last_vid': info.get('last_vid'),  
                                    'mid': info.get('mid')  # 兼容老数据(无mid则为None)  
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
      
    def add_watch(self, group_id: int, up_name: str, last_vid: str = None, mid: int = None):  
        """添加监控"""  
        group_id = str(group_id)  
        if group_id not in self._data:  
            self._data[group_id] = {}  
          
        self._data[group_id][up_name] = {  
            'last_check': datetime.now().isoformat(),  
            'last_vid': last_vid,  
            'mid': mid  
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
      
    def update_last_video(self, group_id: int, up_name: str, last_vid: str, mid: int = None):  
        """更新最后视频记录（可顺带回写mid）"""  
        group_id = str(group_id)  
        if group_id in self._data and up_name in self._data[group_id]:  
            update = {  
                'last_vid': last_vid,  
                'last_check': datetime.now().isoformat()  
            }  
            if mid:  
                update['mid'] = mid  
            self._data[group_id][up_name].update(update)  
            self.save()  
      
    def find_up_by_name(self, name: str) -> Dict[str, str]:  
        """通过名称查找UP主"""  
        return self.name_index.get(normalize_name(name), {})  
  
# 全局存储实例  
watch_storage = UpWatchStorage()  
  
async def get_video_info_with_retry(bvid: str, max_retries: int = 3) -> Optional[Dict]:  
    """带重试的视频信息获取"""  
    for attempt in range(max_retries):  
        try:  
            return await get_video_info(bvid)  
        except Exception as e:  
            if attempt == max_retries - 1:  
                raise  
            await asyncio.sleep(5 * (attempt + 1))  
    return None  
  
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
  
def _clean_search_cache():  
    """清理过期缓存，避免只增不删"""  
    expire_before = datetime.now() - timedelta(minutes=CACHE_EXPIRE_MINUTES)  
    for k in [k for k, (_, ts) in list(search_cache.items()) if ts < expire_before]:  
        del search_cache[k]  
  
async def get_bilibili_search(keyword: str, search_type: str = "video") -> List[Dict]:  
    """统一搜索函数"""  
    _clean_search_cache()  
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
        r'bilibili\.com/video/(av\d+)',  
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
        video_info = await get_video_info_with_retry(bvid)  
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
          
        # 添加到本群监控（同时保存mid，后续检查可直接走空间API）  
        watch_storage.add_watch(  
            group_id=group_id,  
            up_name=up_name,  
            last_vid=bvid,  
            mid=video_info['owner']['mid']  
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
  
async def _fetch_up_videos(session, up_name, mid):  
    """获取某个UP主的最新视频列表；优先空间API，失败才走搜索兜底。  
    返回 (all_videos, resolved_mid)"""  
    all_videos = []  
  
    # 第一步：优先使用空间API查询（有mid就直接查）  
    space_ok = False  
    try:  
        if mid:  
            headers = {  
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',  
                'Referer': f'https://space.bilibili.com/{mid}'  
            }  
            url = f'https://api.bilibili.com/x/space/arc/search?mid={mid}&ps=5&order=pubdate'  
            async with session.get(url, headers=headers, timeout=10) as resp:  
                if resp.status != 200:  
                    raise Exception(f"HTTP {resp.status}")  
                data = await resp.json()  
                if data.get('code') != 0:  
                    if data.get('message') == '请求过于频繁，请稍后再试':  
                        sv.logger.warning(f"空间API触发频控，退避{RATE_LIMIT_BACKOFF}秒")  
                        await asyncio.sleep(RATE_LIMIT_BACKOFF)  
                        raise Exception("API请求过于频繁")  
                    raise Exception(data.get('message', '未知API错误'))  
                  
                vlist = data['data']['list']['vlist']  
                if vlist:  
                    for video in vlist:  
                        video['check_method'] = "空间API"  
                        # 空间API用 created 表示发布时间，统一映射为 pubdate  
                        video['pubdate'] = video.get('pubdate') or video.get('created', 0)  
                    all_videos.extend(vlist)  
                    space_ok = True  
                    sv.logger.info(f"空间API获取到 {len(vlist)} 个视频") 
    except Exception as e:  
        sv.logger.warning(f"空间API查询失败({up_name}): {str(e)}")  
  
    # 空间API成功即短路，跳过所有搜索兜底  
    if space_ok:  
        return all_videos, mid  
  
    # 第二步：使用与"查视频 -up"完全相同的搜索逻辑  
    try:  
        params = {  
            'search_type': 'video',  
            'keyword': up_name,  
            'order': 'pubdate',  
            'ps': MAX_RESULTS * 2,  
            'platform': 'web'  
        }  
        headers = {  
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',  
            'Referer': 'https://www.bilibili.com/',  
            'Cookie': 'buvid3=XXXXXX;'  
        }  
        async with session.get(  
            'https://api.bilibili.com/x/web-interface/search/type',  
            params=params,  
            headers=headers,  
            timeout=10  
        ) as resp:  
            if resp.status != 200:  
                raise Exception(f"HTTP {resp.status}")  
            data = await resp.json()  
            if data.get('code') != 0:  
                raise Exception(data.get('message', '未知API错误'))  
            raw_results = data['data'].get('result', [])  
            if raw_results:  
                matched_videos = []  
                for video in raw_results:  
                    if normalize_name(video['author']) == normalize_name(up_name):  
                        video['check_method'] = "搜索API(查视频-up)"  
                        matched_videos.append(video)  
                if matched_videos:  
                    all_videos.extend(matched_videos)  
                    sv.logger.info(f"搜索API(查视频-up)获取到 {len(matched_videos)} 个视频")  
    except Exception as e:  
        sv.logger.warning(f"搜索API(查视频-up)失败({up_name}): {str(e)}")  
  
    # 第三步：使用普通搜索  
    try:  
        results = await get_bilibili_search(up_name)  
        if results:  
            matched_videos = []  
            for video in results:  
                if normalize_name(video['author']) == normalize_name(up_name):  
                    video['check_method'] = "直接搜索(查视频+UP名)"  
                    matched_videos.append(video)  
            if matched_videos:  
                all_videos.extend(matched_videos)  
                sv.logger.info(f"直接搜索(查视频+UP名)获取到 {len(matched_videos)} 个视频")  
    except Exception as e:  
        sv.logger.warning(f"直接搜索(查视频+UP名)失败({up_name}): {str(e)}")  
  
    # 第四步：使用UP名+最新作为关键词  
    try:  
        results = await get_bilibili_search(f"{up_name} 最新")  
        if results:  
            matched_videos = []  
            for video in results:  
                if normalize_name(video['author']) == normalize_name(up_name):  
                    video['check_method'] = "关键词搜索(UP名+最新)"  
                    matched_videos.append(video)  
            if matched_videos:  
                all_videos.extend(matched_videos)  
                sv.logger.info(f"关键词搜索(UP名+最新)获取到 {len(matched_videos)} 个视频")  
    except Exception as e:  
        sv.logger.warning(f"关键词搜索(UP名+最新)失败({up_name}): {str(e)}")  
  
    # 第五步：使用UP名+年份作为关键词  
    try:  
        current_year = datetime.now().year  
        results = await get_bilibili_search(f"{up_name} {current_year}")  
        if results:  
            matched_videos = []  
            for video in results:  
                if normalize_name(video['author']) == normalize_name(up_name):  
                    video['check_method'] = "关键词搜索(UP名+年份)"  
                    matched_videos.append(video)  
            if matched_videos:  
                all_videos.extend(matched_videos)  
                sv.logger.info(f"关键词搜索(UP名+年份)获取到 {len(matched_videos)} 个视频")  
    except Exception as e:  
        sv.logger.warning(f"关键词搜索(UP名+年份)失败({up_name}): {str(e)}")  
  
    return all_videos, mid  
  
@sv.scheduled_job('interval', minutes=UP_WATCH_INTERVAL)  
async def check_up_updates():  
    """定时检查UP主更新（增强版·去重优化）"""  
    start_time = time.time()  
    sv.logger.info("开始执行UP主监控检查...")  
    all_watches = watch_storage.get_all_watches()  
    if not all_watches:  
        sv.logger.info("当前没有监控任何UP主")  
        return  
      
    bot = sv.bot  
    update_count = 0  
  
    # 跨群按 UP 去重：同一 UP 只查一次 API，再分发到各关注群  
    # up_registry: {up_name: {'mid': mid, 'groups': {group_id(int): last_vid}}}  
    up_registry: Dict[str, Dict[str, Any]] = {}  
    for group_id_str, up_dict in all_watches.items():  
        group_id = int(group_id_str)  
        for up_name, info in up_dict.items():  
            entry = up_registry.setdefault(up_name, {'mid': info.get('mid'), 'groups': {}})  
            if not entry['mid'] and info.get('mid'):  
                entry['mid'] = info.get('mid')  
            entry['groups'][group_id] = info.get('last_vid')  
  
    total_ups = len(up_registry)  
  
    async with aiohttp.ClientSession() as session:  # 复用同一个session  
        for up_name, entry in up_registry.items():  
            try:  
                # 每个UP主之间的延迟（由固定30秒缩短，且按UP维度而非群维度）  
                await asyncio.sleep(UP_CHECK_DELAY)  
  
                mid = entry['mid']  
  
                # 若没有mid，用任一群的last_vid反查一次拿到mid  
                if not mid:  
                    any_vid = next((v for v in entry['groups'].values() if v), None)  
                    if any_vid:  
                        try:  
                            vinfo = await get_video_info_with_retry(any_vid)  
                            if vinfo:  
                                mid = vinfo['owner']['mid']  
                        except Exception as e:  
                            sv.logger.warning(f"反查mid失败({up_name}): {str(e)}")  
  
                sv.logger.info(f"开始检查UP主【{up_name}】更新，mid: {mid or '未知'}")  
  
                # 获取该UP主最新视频（空间API优先，失败才走搜索兜底）  
                all_videos, mid = await _fetch_up_videos(session, up_name, mid)  
  
                # 去重并排序所有视频  
                unique_videos = {}  
                for video in all_videos:  
                    bvid = video.get('bvid')  
                    if not bvid:  
                        continue  
                    video_key = f"{bvid}_{video.get('title','')}_{video.get('author','')}"  
                    if video_key not in unique_videos or video.get('pubdate', 0) > unique_videos[video_key].get('pubdate', 0):  
                        unique_videos[video_key] = video
  
                sorted_videos = sorted(unique_videos.values(),  
                                       key=lambda x: x.get('pubdate', 0),  
                                       reverse=True)
  
                if not sorted_videos:  
                    sv.logger.info(f"无法获取【{up_name}】的任何视频信息")  
                    continue  
  
                latest_video = sorted_videos[0]  
                current_bvid = latest_video['bvid']  
                check_method = latest_video.get('check_method', '未知方法')  
                video_pub_time = datetime.fromtimestamp(latest_video['pubdate'])  
  
                # 对每个关注该UP的群，分别用该群的last_vid判断是否新视频  
                for group_id, last_vid in entry['groups'].items():  
                    try:  
                        is_new = False  
                        reason = ""  
  
                        if not last_vid:  
                            is_new = True  
                            reason = "首次监控该UP主"  
                        else:  
                            # 情况1：BV号相同但可能是重新上传  
                            if current_bvid == last_vid:  
                                last_video_info = await get_video_info_with_retry(last_vid)  
                                if not last_video_info:  
                                    reason = "无法获取上次视频信息，保守处理不推送"  
                                else:  
                                    last_pub_time = datetime.fromtimestamp(last_video_info['pubdate'])  
                                    if abs((video_pub_time - last_pub_time).total_seconds()) > 3600:  
                                        is_new = True  
                                        reason = "BV号相同但发布时间差异大，可能是重新上传"  
                                    else:  
                                        reason = "BV号相同且发布时间相近，视为同一视频"  
                            # 情况2：BV号不同  
                            else:  
                                last_video_info = await get_video_info_with_retry(last_vid)  
                                if not last_video_info:  
                                    is_new = True  
                                    reason = "无法验证上次视频，保守推送新视频"  
                                else:  
                                    last_pub_time = datetime.fromtimestamp(last_video_info['pubdate'])  
                                    title_changed = latest_video.get('title') != last_video_info.get('title')  
                                    time_diff = (video_pub_time - last_pub_time).total_seconds()  
                                    if time_diff > 300:  # 5分钟阈值  
                                        is_new = True  
                                        reason = f"新视频发布时间({video_pub_time})比上次({last_pub_time})晚{time_diff/60:.1f}分钟"  
                                    elif title_changed and time_diff > -300:  # 允许5分钟误差  
                                        is_new = True  
                                        reason = "标题不同且发布时间相近，视为新视频"  
                                    else:  
                                        reason = "无新发布(未满足推送条件)"  
  
                        sv.logger.info(f"视频检查详情:\n"  
                                       f"群: {group_id}\n"  
                                       f"UP主: {up_name}\n"  
                                       f"上次视频: {last_vid or '无'}\n"  
                                       f"最新视频: {current_bvid}\n"  
                                       f"发布时间: {video_pub_time}\n"  
                                       f"检查方法: {check_method}\n"  
                                       f"判断结果: {reason}")  
  
                        if is_new:  
                            # 更新记录并顺带回写mid（给老数据补mid）  
                            watch_storage.update_last_video(  
                                group_id=group_id,  
                                up_name=up_name,  
                                last_vid=current_bvid,  
                                mid=mid  
                            )  
  
                            pub_time = video_pub_time.strftime("%Y-%m-%d %H:%M")  
                            pic_url = process_pic_url(latest_video['pic'])  
                            msg = [  
                                f"📢 UP主【{up_name}】发布了新视频！",  
                                f"📺 标题: {latest_video['title']}",  
                                f"[CQ:image,file={pic_url}]",  
                                f"⏰ 发布时间: {pub_time}",  
                                f"🔗 视频链接: https://b23.tv/{current_bvid}",  
                                f"🔍 检查方式: {check_method}"  
                            ]  
                            await bot.send_group_msg(group_id=group_id, message="\n".join(msg))  
                            update_count += 1  
                            sv.logger.info(f"已发送新视频通知: 群{group_id} {up_name} - {latest_video['title']}")  
                    except Exception as e:  
                        sv.logger.error(f'群{group_id}推送UP主【{up_name}】失败: {str(e)}')  
                        continue  
  
            except Exception as e:  
                sv.logger.error(f'监控UP主【{up_name}】失败: {str(e)}')  
                continue  
  
    elapsed_minutes = (time.time() - start_time) / 60  
    success_rate = (update_count / total_ups * 100) if total_ups else 0  
    sv.logger.info(f"监控检查完成，耗时{elapsed_minutes:.1f}分钟\n"  
                   f"共检查 {total_ups} 个UP主(去重后)\n"  
                   f"发现 {update_count} 个更新\n"  
                   f"更新率 {success_rate:.1f}%")  
  
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