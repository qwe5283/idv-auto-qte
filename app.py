"""
Identity V QTE Auto-Handler (Vision Edition)
"""

import cv2
import numpy as np
import math
import time
import sys
import ctypes
import mss
import pydirectinput
import psutil
import pywintypes
from dataclasses import dataclass, field
from collections import deque
from typing import Optional, Tuple
from ctypes import wintypes

# 尝试导入 win32gui，若失败则提示
try:
    import win32gui
    import win32con
except ImportError:
    print("缺少依赖：请运行 pip install pywin32")
    sys.exit(1)

# ==========================================
# 1. DPI 感知设置（必须在所有窗口/GDI 操作之前调用）
# ==========================================
def _enable_dpi_awareness():
    """
    启用进程级 DPI 感知，确保 GetClientRect / ClientToScreen / mss 截图
    返回的坐标均为物理像素，而非 Windows DPI 虚拟化后的逻辑像素。
    必须在导入 win32gui 之后、首次调用任何窗口 API 之前执行。
    """
    try:
        # Per-Monitor DPI Awareness (Win 8.1), 适配多显示器不同缩放
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        print("[!] 警告：无法启用 DPI 感知，高 DPI 缩放下可能导致截图功能异常！")

_enable_dpi_awareness()


# ==========================================
# 2. 配置中心
# ==========================================
@dataclass
class Config:
    """全局配置中心，所有可调参数集中管理"""
    # 进程与窗口
    TARGET_PROCESS_NAME: str = "dwrg.exe"
    
    # 红色HSV范围（两段）
    RED_LOWER1: np.ndarray = field(default_factory=lambda: np.array([0, 157, 90]))
    RED_UPPER1: np.ndarray = field(default_factory=lambda: np.array([8, 255, 255]))
    RED_LOWER2: np.ndarray = field(default_factory=lambda: np.array([175, 157, 90]))
    RED_UPPER2: np.ndarray = field(default_factory=lambda: np.array([180, 255, 255]))
    
    # 黄色HSV范围
    YELLOW_LOWER: np.ndarray = field(default_factory=lambda: np.array([14, 70, 140]))
    YELLOW_UPPER: np.ndarray = field(default_factory=lambda: np.array([23, 140, 255]))
    
    # 圆弧归一化几何参数 (基于16:9比例)
    ARC_CENTER: Tuple[float, float] = (0.504, 0.834)
    RADIUS: float = 0.313
    THICKNESS: float = 0.02
    START_ANGLE: int = 200
    END_ANGLE: int = 340
    
    # 追踪器参数
    # （游戏帧率上限为60FPS，考虑游戏引擎输入队列轮询延迟）
    SYSTEM_DELAY_MS: float = 15.0         # 延迟补偿时间（结合云游戏延迟换算），提前触发
    COOLDOWN_SEC: float = 1.5             # QTE击打触发后的冷却时间（秒）
    RED_TIME_WINDOW_SEC: float = 0.4      # 红色指针运动趋势的采样时间窗口（秒），仅用于维护计算红色指针角速度所用队列
    # （游戏中的红色指针的速度通常在120度/秒左右）
    MIN_ANGULAR_SPEED_DPS: float = 60.0   # 红色指针的最小角速度阈值（度/秒），转动过慢将被忽略
    # （新出现的红色指针应位于圆弧左侧，角度小于此阈值才视为合法QTE指针，排除场景红色物体误判）
    NEW_RED_MAX_ANGLE: float = 215.0      # 红色指针首次出现时的最大允许角度（度）
    # （第五人格的完美校准（角度）黄色范围通常为5度左右）
    MIN_YELLOW_SPAN_DEG: float = 4.0      # 允许的最小QTE黄色区域范围（角），将过滤掉小于此角度的黄色区域
    MAX_YELLOW_SPAN_DEG: float = 10.0     # 允许的最大QTE黄色区域范围（角），将过滤掉大于此角度的黄色区域
    # （锁存黄色区域并延迟消失，防止红色指针盖住黄色区域影响HSV范围导致无法识别黄色区域）
    # （锁存后黄色区域短暂识别失败也能正常QTE）
    YELLOW_LAG_SEC: float = 0.4           # 黄色区域稳定与滞后时间（秒），设置过大可能导致错过角度较小的QTE
    # （要求黄色区域持续存在YELLOW_LAG_SEC秒且时间窗口内角度波动变化都稳定在YELLOW_STABLE_TOLERANCE度内，才算作有效QTE范围，防止场景中漂浮的粒子进入ROI影响识别误判）
    YELLOW_STABLE_TOLERANCE: float = 0.3  # 黄色区域在稳定时间窗口内的允许波动的角度范围阈值
    # 红色指针特征过滤参数
    RED_INNER_EXTEND_MULT: float = 4.0    # 红色指针检测时，向内拓展厚度的倍数（构建超集ROI在更大范围检测红色指针）
    RED_RADIAL_SEGMENTS: int = 8          # 红色指针检测时，径向分段数（3=内/中/外环，2=内/外环）。段数越多抗噪越强，但易漏检细指针

    # 预览
    PREVIEW_VIDEO_HIT_TIME_SEC: float = 3 # 视频分析模式下，停留预览命中结果展示的时长（秒）
    PREVIEW_WINDOW_TOP_MOST: bool = True  # 预览窗口是否置顶
    
    # 比例调整和最大屏幕分辨率
    TARGET_ASPECT_RATIO: float = 16 / 9
    MAX_PROCESS_WIDTH = 1280
    MAX_PROCESS_HEIGHT = 720


# ==========================================
# 3. 窗口管理器
# ==========================================
class WindowManager:
    """处理Windows窗口查找、焦点判断、尺寸获取与调整"""

    # 定义 ctypes 调用 Windows API 的函数签名
    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

    @staticmethod
    def get_top_level_hwnd(hwnd: int) -> int:
        """获取指定窗口的顶层父窗口"""
        while True:
            parent = win32gui.GetParent(hwnd)
            if not parent:
                break
            hwnd = parent
        return hwnd
    
    @staticmethod
    def get_mumu_render_hwnd(mumu_top_hwnd: int) -> int:
        """传入MuMu顶层窗口, 获取渲染画面子窗口句柄"""
        return win32gui.FindWindowEx(mumu_top_hwnd, None, "Qt5156QWindowIcon", "MuMuNxDevice")

    @staticmethod
    def find_mumu_hwnd() -> Optional[int]:
        """查找 MuMu 模拟器的顶层窗口"""
        hwnd = win32gui.FindWindow("Qt5156QWindowIcon", "MuMu安卓设备")
        return hwnd if hwnd else None

    @staticmethod
    def get_dpi_scale(hwnd: int = 0) -> float:
        """
        获取指定窗口（或系统默认）的 DPI 缩放因子，相对于 96 DPI。
        返回例如 1.0 / 1.25 / 1.5 / 2.0。
        """
        try:
            # Windows 10 1607+ : GetDpiForWindow
            dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
            if dpi > 0:
                return dpi / 96.0
        except (AttributeError, Exception):
            pass
        try:
            # 回退: GetDeviceCaps(LOGPIXELSX)
            hdc = ctypes.windll.user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(0, hdc)
            if dpi > 0:
                return dpi / 96.0
        except Exception:
            pass
        return 1.0

    @staticmethod
    def is_admin() -> bool:
        try:
            return ctypes.windll.shell32.IsUserAnAdmin()
        except:
            return False

    @staticmethod
    def find_hwnd_by_pid(pid: int) -> Optional[int]:
        def callback(hwnd, hwnds):
            if win32gui.IsWindowVisible(hwnd) and win32gui.IsWindowEnabled(hwnd):
                found_pid = wintypes.DWORD()
                WindowManager.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(found_pid))
                if found_pid.value == pid:
                    hwnds.append(hwnd)
            return True
        hwnds = []
        win32gui.EnumWindows(callback, hwnds)
        return hwnds[0] if hwnds else None

    def wait_for_focus(self, process_name: str) -> Tuple[int, str]:
        """同步阻塞，等待游戏客户端或 MuMu 模拟器成为焦点窗口，返回(顶层窗口句柄, 客户端类型)"""
        print(f"[*] 等待进程 [{process_name}] 或 [MuMu模拟器] 启动并获取焦点...")
        while True:
            # 1. 尝试查找本地游戏进程
            pid = None
            for proc in psutil.process_iter(['pid', 'name']):
                if proc.info['name'] and proc.info['name'].lower() == process_name.lower():
                    pid = proc.info['pid']
                    break
            if pid:
                hwnd = self.find_hwnd_by_pid(pid)
                if hwnd and win32gui.GetForegroundWindow() == hwnd:
                    print(f"[+] 已检测到游戏客户端焦点窗口: HWND={hwnd}")
                    return hwnd, "CLIENT"
            # 2. 未找到本地游戏，尝试寻找 MuMu 模拟器
            mumu_top_hwnd = self.find_mumu_hwnd()
            if mumu_top_hwnd:
                render_hwnd = self.get_mumu_render_hwnd(mumu_top_hwnd) # 模拟器未启动完成时可能无子渲染窗口
                fg_hwnd = win32gui.GetForegroundWindow()
                if render_hwnd and fg_hwnd == mumu_top_hwnd:
                        print(f"[+] 已检测到 MuMu 模拟器窗口: HWND={mumu_top_hwnd}")
                        return mumu_top_hwnd, "EMULATOR"
            time.sleep(0.5)

    def get_client_rect(self, hwnd: int) -> Tuple[int, int, int, int]:
        """获取客户区在屏幕上的绝对坐标"""
        client_rect = win32gui.GetClientRect(hwnd)
        left, top = win32gui.ClientToScreen(hwnd, (client_rect[0], client_rect[1]))
        right, bottom = win32gui.ClientToScreen(hwnd, (client_rect[2], client_rect[3]))
        return left, top, right, bottom
    
    def resize_window_to_16_9(self, hwnd: int, current_w: int, current_h: int):
        """调整游戏窗口大小为16:9"""
        # 检查窗口是否最大化，若是则先恢复为普通状态
        placement = win32gui.GetWindowPlacement(hwnd)
        if placement[1] == win32con.SW_MAXIMIZE:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.5)  # 等待窗口恢复
            left, top, right, bottom = self.get_client_rect(hwnd)
            current_w, current_h = right - left, bottom - top

        window_rect = win32gui.GetWindowRect(hwnd)
        border_x = (window_rect[2] - window_rect[0]) - current_w
        border_y = (window_rect[3] - window_rect[1]) - current_h
        # 保持高度不变，重新计算宽度
        new_h = current_h
        new_w = int(current_h * Config.TARGET_ASPECT_RATIO)
        
        screen_w = self.user32.GetSystemMetrics(0)
        screen_h = self.user32.GetSystemMetrics(1)
        if new_h + border_y > screen_h:
            new_h = screen_h - border_y
            new_w = int(new_h * Config.TARGET_ASPECT_RATIO)
            
        win32gui.SetWindowPos(hwnd, None, window_rect[0], window_rect[1], 
                              new_w + border_x, new_h + border_y, win32con.SWP_NOZORDER)
        print(f"[+] 窗口已调整为: 客户区 {new_w}x{new_h}")
        time.sleep(0.5)

# ==========================================
# 4. 视觉检测器
# ==========================================
class QTEDetector:
    """负责图像处理，掩膜生成与颜色极角提取"""
    def __init__(self, width: int, height: int, config: Config):
        self.cfg = config
        # 将归一化坐标转换为物理坐标
        self.arc_center = (int(self.cfg.ARC_CENTER[0] * width), int(self.cfg.ARC_CENTER[1] * height))
        self.radius = int(self.cfg.RADIUS * height)
        self.thickness = int(self.cfg.THICKNESS * height)
        # 生成掩膜并从掩膜面积计算噪点面积过滤阈值
        self.arc_mask, self.arc_mask_area = self._generate_circular_arc_mask(width, height)
        self.red_inner_radius = max(self.radius - int(self.thickness * self.cfg.RED_INNER_EXTEND_MULT), 1) # 计算向内拓展的红色检测内半径
        self.min_yellow_area = max(1, int(self.arc_mask_area * 0.025))
        # 计算模糊预处理强度
        k_size = max(3, int(height / 300))
        if k_size % 2 == 0: k_size += 1
        self.kernel_noise = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_size, k_size))
        # 切片出ROI以减少计算量
        self.arc_roi = cv2.boundingRect(self.arc_mask) # (x, y, w, h)
        rx, ry, rw, rh = self.arc_roi
        self.arc_mask_roi = self.arc_mask[ry:ry+rh, rx:rx+rw]

    def _generate_circular_arc_mask(self, width: int, height: int) -> Tuple[np.ndarray, int]:
        """初始化时调用，生成C字形掩膜"""
        mask = np.zeros((height, width), dtype=np.uint8)
        center, radius = self.arc_center, self.radius
        inner_radius = max(radius - self.thickness, 1)
        
        angles = np.linspace(np.radians(self.cfg.START_ANGLE), np.radians(self.cfg.END_ANGLE), 
                             num=int((self.cfg.END_ANGLE - self.cfg.START_ANGLE) * 2 + 1))
        cos_a, sin_a = np.cos(angles), np.sin(angles)
        
        outer_x = center[0] + radius * cos_a
        outer_y = center[1] + radius * sin_a
        inner_x = center[0] + inner_radius * cos_a
        inner_y = center[1] + inner_radius * sin_a
        
        polygon_pts = np.column_stack((
            np.concatenate([outer_x, inner_x[::-1]]),
            np.concatenate([outer_y, inner_y[::-1]])
        )).astype(np.int32)
        
        cv2.fillPoly(mask, [polygon_pts], 255)
        return mask, cv2.countNonZero(mask)
    
    def process_frame(self, frame: np.ndarray, is_already_roi: bool = False) -> Tuple[Optional[float], Optional[Tuple[float, float]], Optional[np.ndarray]]:
        """对传入帧图像进行颜色识别并提取角度。只在ROI区域做HSV转换。
        
        Args:
            frame: 传入帧图像。
            is_already_roi: 传入的图像是否已经进行ROI切片。

        Returns:
            (红色指针的任意角角度, (黄色目标范围起始任意角角度, 黄色目标范围结束任意角角度))

        """
        rx, ry, rw, rh = self.arc_roi
        h_frame, w_frame = frame.shape[:2]

        if not is_already_roi:
            frame_roi = frame[ry:ry+rh, rx:rx+rw]
            hsv_roi = cv2.cvtColor(frame_roi, cv2.COLOR_BGR2HSV)
        else:
            hsv_roi = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        # 黄色提取
        mask_yellow = cv2.inRange(hsv_roi, self.cfg.YELLOW_LOWER, self.cfg.YELLOW_UPPER)
        mask_yellow = cv2.morphologyEx(mask_yellow, cv2.MORPH_CLOSE, self.kernel_noise)
        mask_yellow = cv2.bitwise_and(mask_yellow, mask_yellow, mask=self.arc_mask_roi)

        # 红色提取
        mask_red = cv2.bitwise_or(
            cv2.inRange(hsv_roi, self.cfg.RED_LOWER1, self.cfg.RED_UPPER1),
            cv2.inRange(hsv_roi, self.cfg.RED_LOWER2, self.cfg.RED_UPPER2)
        )
        mask_red = cv2.morphologyEx(mask_red, cv2.MORPH_CLOSE, self.kernel_noise)

        red_angle, debug_red_mask_roi = self._get_red_front_angle(mask_red, rx, ry)
        yellow_span = self._get_yellow_angle_span(mask_yellow, rx, ry)

        # 统一将 debug_red_mask 映射回与输入 frame 相同的尺寸
        full_debug_red_mask = np.zeros((h_frame, w_frame), dtype=np.uint8)
        if debug_red_mask_roi is not None and np.any(debug_red_mask_roi):
            if is_already_roi:
                # 传入的已经是 ROI，直接赋值
                full_debug_red_mask[:debug_red_mask_roi.shape[0], :debug_red_mask_roi.shape[1]] = debug_red_mask_roi
            else:
                # 传入的是全图，放回对应的 ROI 位置
                h_roi, w_roi = debug_red_mask_roi.shape[:2]
                full_debug_red_mask[ry:ry+h_roi, rx:rx+w_roi] = debug_red_mask_roi

        return red_angle, yellow_span, full_debug_red_mask

    def _get_red_front_angle(self, mask_red: np.ndarray, rx: int, ry: int) -> Tuple[Optional[float], np.ndarray]:
        """计算红色指针前沿的任意角范围角度（N段同心环交集法）
        
        将连续的极角映射到0-359的离散数组，将拓展后的环形检测区分为外半环和内半环（2段时），分别统计外半环和内半环中，哪些1度区间存在红色像素
        取交集：只有当一个角度区间在内外半环同时存在红色时，才认定该区间是指针的一部分
        利用布尔索引，直接从原图中提取出属于这些有效区间的像素，取过滤后像素块中最大的角度作为指针前沿的任意角度。
        """
        ys, xs = np.where(mask_red > 0)
        debug_mask = np.zeros_like(mask_red, dtype=np.uint8) # 初始化Debug掩膜
        if len(xs) == 0:
            return None, debug_mask
        
        # 计算局部坐标系下的圆弧中心
        local_center = (self.arc_center[0] - rx, self.arc_center[1] - ry)        
        # 1. 计算极坐标
        dx = xs - local_center[0]
        dy = ys - local_center[1]
        dist = np.sqrt(dx**2 + dy**2)
        angles = np.degrees(np.arctan2(dy, dx))
        angles[angles < 0] += 360
        # 2. 过滤掉不在拓展环形区域 [inner_r, outer_r] 内的像素
        mask_dist = (dist >= self.red_inner_radius) & (dist <= self.radius)
        dist = dist[mask_dist]
        angles = angles[mask_dist]
        # 同步过滤 xs, ys 用于后续掩膜赋值
        xs = xs[mask_dist]
        ys = ys[mask_dist]

        if len(angles) == 0:
            return None, debug_mask

        # 3. 将环形区域径向分段
        N = self.cfg.RED_RADIAL_SEGMENTS
        boundaries = np.linspace(self.red_inner_radius, self.radius, N + 1)
        segment_indices = np.digitize(dist, boundaries[1:]) # np.digitize 返回每个 dist 属于哪个 bin 的索引 (1 到 N)
        # 极角1度离散化
        angle_bins = np.floor(angles).astype(int)
        valid_bins = (angle_bins >= 0) & (angle_bins < 360)
        # 4. 标记每个分段中存在的角度区间
        segment_present = np.zeros((N, 360), dtype=bool)
        for i in range(N):
            mask = (segment_indices == i) & valid_bins
            segment_present[i, angle_bins[mask]] = True
        # 5. 核心逻辑：取所有分段的交集。必须贯穿所有环，才算指针
        angle_valid = np.all(segment_present, axis=0) # 形状 (360,)
        # 6. 将布尔结果映射回原始像素，提取前沿浮点角度
        valid_pixel_mask = np.zeros(len(angles), dtype=bool)
        valid_pixel_mask[valid_bins] = angle_valid[angle_bins[valid_bins]]
        
        if not np.any(valid_pixel_mask):
            return None, debug_mask
            
        # 7. 提取所有贯穿区间的像素，取最大浮点角度作为顺时针前沿
        max_angle = np.max(angles[valid_pixel_mask])
        # 8. 生成 Debug 掩膜 (仅保留通过贯穿测试的指针像素)
        debug_mask[ys[valid_pixel_mask], xs[valid_pixel_mask]] = 255

        return max_angle, debug_mask

    def _get_yellow_angle_span(self, mask_yellow: np.ndarray, rx: int, ry: int) -> Optional[Tuple[float, float]]:
        """计算黄色目标区域的起始任意角和结束任意角范围"""
        contours, _ = cv2.findContours(mask_yellow, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        # 取面积最大的轮廓，滤除散点噪声
        max_contour = max(contours, key=cv2.contourArea)
        if cv2.contourArea(max_contour) < self.min_yellow_area:
            return None
        # 计算局部坐标系下的圆弧中心
        local_center = (self.arc_center[0] - rx, self.arc_center[1] - ry)
        # 展平轮廓点并计算每个点相对中心的极角
        points = max_contour.reshape(-1, 2)
        dx = points[:, 0] - local_center[0]
        dy = points[:, 1] - local_center[1]
        angles = np.degrees(np.arctan2(dy, dx))
        angles[angles < 0] += 360 # 映射到 [0, 360)
        return (np.min(angles), np.max(angles))
    
    def render_debug(self, 
                     frame: np.ndarray, 
                     red_angle: Optional[float], 
                     yellow_span: Optional[Tuple[float, float]], 
                     status_msg: str, 
                     is_hit: bool,
                     debug_red_mask: Optional[np.ndarray] = None, 
                     is_already_roi: bool = False) -> np.ndarray:
        """负责所有的Debug绘制，返回渲染后的图像"""
        vis_frame = frame.copy()
        color = (0, 255, 0) if is_hit else (255, 255, 255)
        
        # 根据是否为ROI，计算绘制中心和选择掩膜
        rx, ry, rw, rh = self.arc_roi
        if is_already_roi:
            draw_center = (self.arc_center[0] - rx, self.arc_center[1] - ry)
            draw_mask = self.arc_mask_roi
            # ROI 图像较小，缩小字体和线条防止溢出
            text_scale, text_thickness, status_pos = 0.6, 2, (20, 50)
        else:
            draw_center = self.arc_center
            draw_mask = self.arc_mask
            text_scale, text_thickness, status_pos = 1.2, 3, (300, 200)

        # 绘制状态
        cv2.putText(vis_frame, status_msg, status_pos, cv2.FONT_HERSHEY_SIMPLEX, text_scale, color, text_thickness)
        
        # 绘制中间过程产物：通过贯穿测试的红色指针掩膜
        if debug_red_mask is not None and np.any(debug_red_mask):
            # 创建一个纯洋红色 (255, 0, 255) 的叠加层
            overlay = np.zeros_like(vis_frame, dtype=np.uint8)
            overlay[debug_red_mask > 0] = (255, 0, 255)
            # 将掩膜以半透明方式叠加到原图上，让用户看清是指针的哪部分被识别
            cv2.addWeighted(vis_frame, 1.0, overlay, 0.6, 0, vis_frame)

        # 绘制指针
        if red_angle is not None:
            rad = math.radians(red_angle)
            end_x = int(draw_center[0] + self.radius * math.cos(rad))
            end_y = int(draw_center[1] + self.radius * math.sin(rad))
            cv2.circle(vis_frame, (end_x, end_y), 4, (0, 0, 255), -1)
            if status_msg != "Red Not On Left Side":
                cv2.line(vis_frame, draw_center, (end_x, end_y), (0, 0, 255), 2)
            
        # 绘制掩膜轮廓
        contours, _ = cv2.findContours(draw_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(vis_frame, contours, -1, (255, 255, 255), 1)

        # 绘制黄色目标
        if yellow_span:
            for angle in yellow_span:
                rad = math.radians(angle)
                end_x = int(draw_center[0] + self.radius * math.cos(rad))
                end_y = int(draw_center[1] + self.radius * math.sin(rad))
                cv2.line(vis_frame, draw_center, (end_x, end_y), (0, 255, 255), 2)

        return vis_frame


# ==========================================
# 5. 状态追踪器
# ==========================================
class QTETracker:
    """QTE状态机，负责运动趋势分析与击打预判"""

    def __init__(self, config: Config):
        self.cfg = config
        self.triggered = False
        self.last_trigger_time = 0.0  # 记录上次触发的时间戳用于计算冷却
        
        self.red_angle_history = deque() # 存储 (角度, 时间戳)
        self.angular_speed = 0.0 # 度/秒

        self.yellow_history = deque()  # 存储 (角度跨度, 时间戳)
        self.locked_yellow_span = None
        self.status_msg = "Waiting"

    def update_and_check(self, red_front_angle: Optional[float], yellow_span: Optional[Tuple[float, float]], delay_ms: float = 0.0) -> bool:
        """返回是否应该触发按键"""
        current_time = time.perf_counter()

        if self.triggered:
            if current_time - self.last_trigger_time >= self.cfg.COOLDOWN_SEC: # 冷却结束
                self.triggered = False
                self.yellow_history.clear()
                self.red_angle_history.clear()
            else: # 冷却中
                self.status_msg = "Cooldown"
                return False

        if red_front_angle is None: # 没有检测到红色指针
            self.status_msg = "No Red"
            self.red_angle_history.clear()
            if self.yellow_history and current_time - self.yellow_history[-1][1] > self.cfg.YELLOW_LAG_SEC:
                # 黄色区域的最新采样时间不在时间窗口内（已过期），则清除锁存
                self.yellow_history.clear()
                self.locked_yellow_span = None
            return False
            
        # 新红色指针出现时（历史为空），验证其是否在圆弧左侧
        if len(self.red_angle_history) == 0 and red_front_angle >= self.cfg.NEW_RED_MAX_ANGLE:
            self.status_msg = "Red Not On Left Side"
            return False

        # 检测红色指针是否停止转动
        self.red_angle_history.append((red_front_angle, current_time))
        if not self._check_red_moving_right(current_time):
            self.status_msg = "Red Not Moving/Too Slow"
            return False

        # 更新黄色目标区域状态
        if not self._update_yellow_state(yellow_span, current_time):
            self.status_msg = "Yellow Missing/Unstable"
            return False

        if self.locked_yellow_span is None:
            return False

        # 基于到达时间的预判
        target_angle = self.locked_yellow_span[0]
        # 延迟补偿 (self.angular_speed 已经是 度/秒)
        delay_compensation_angle = self.angular_speed * (delay_ms / 1000.0)
        # 拿到延迟补偿后当前指针所指角度
        current_projected_angle = red_front_angle + delay_compensation_angle
        
        if current_projected_angle >= target_angle: # 判定指针已经到达目标
            self.triggered = True
            self.last_trigger_time = current_time
            self.status_msg = ">>> HIT! SPACE <<<"
            self.red_angle_history.clear()
            self.locked_yellow_span = None
            print(f"[DEBUG] Red Angular Speed: {self.angular_speed:.1f} deg/s")
            return True
            
        self.status_msg = f"Approaching... R:{red_front_angle:.1f} T:{target_angle:.1f}"
        return False

    def _check_red_moving_right(self, current_time: float) -> bool:
        """检测红色指针是否正在向右顺时针旋转"""
        # 清理超过时间窗口的红色历史记录
        while self.red_angle_history and current_time - self.red_angle_history[0][1] > self.cfg.RED_TIME_WINDOW_SEC:
            self.red_angle_history.popleft()
        if len(self.red_angle_history) < 5:
            return False # 至少5个采样点再开始计算速度
        
        speeds = []
        for i in range(1, len(self.red_angle_history)):
            prev_angle, prev_time = self.red_angle_history[i-1]
            curr_angle, curr_time = self.red_angle_history[i]
            delta_time = curr_time - prev_time
            delta_angle = curr_angle - prev_angle
            if delta_angle <= 0:
                continue # 只考虑顺时针旋转的样本，过滤掉但渲染帧重复采样造成不动的情况
            speeds.append(delta_angle / delta_time) # 计算每个连续样本的瞬时角速度
        if not speeds:  
            return False  # 时间窗口内没有足够有效的跨帧样本计算速度
        # 计算瞬时速度的中位数，防止异常值或重复采样造成的速度毛刺干扰
        median_speed = float(np.median(speeds))
        if median_speed > self.cfg.MIN_ANGULAR_SPEED_DPS:
            self.angular_speed = median_speed
            return True
        return False
    
    def _update_yellow_state(self, current_yellow_span: Optional[Tuple[float, float]], current_time: float) -> bool:
        """记录和维护黄色区域的锁存状态，排除黄色噪点引发的干扰，返回黄色区域状态是否靠谱且可用"""
        # 清理超过稳定时间窗口的黄色历史记录
        while self.yellow_history and current_time - self.yellow_history[0][1] > self.cfg.YELLOW_LAG_SEC:
            self.yellow_history.popleft()
        if current_yellow_span is not None: # 当前这一帧存在检测到的黄色区域
            # 记录当前黄色区域信息
            self.yellow_history.append((current_yellow_span, current_time))
            # 检查在时间窗口内是否有足够稳定的记录
            if len(self.yellow_history) >= 2:
                starts = [s[0][0] for s in self.yellow_history] # 时间窗口内所有黄色区域的起始角度列表
                ends = [s[0][1] for s in self.yellow_history] # 时间窗口内所有黄色区域的结束角度列表
                if (max(starts)-min(starts) <= self.cfg.YELLOW_STABLE_TOLERANCE and max(ends)-min(ends) <= self.cfg.YELLOW_STABLE_TOLERANCE):
                    # 稳定时间窗口内记录黄色区域的所有角度都稳定在若差范围内
                    span_width = current_yellow_span[1] - current_yellow_span[0]
                    if self.cfg.MIN_YELLOW_SPAN_DEG < span_width < self.cfg.MAX_YELLOW_SPAN_DEG: # 过滤掉（角度）范围过小或过大的黄色噪点
                        # 锁存满足条件的状态
                        self.locked_yellow_span = current_yellow_span
            return self.locked_yellow_span is not None # 返回锁存信息存在状态
        else: # 当前这一帧没有检测到黄色区域
            if self.locked_yellow_span is not None: # 存在锁存信息
                if self.yellow_history and current_time - self.yellow_history[-1][1] < self.cfg.YELLOW_LAG_SEC:
                    # 最新记录的时间戳处于稳定时间窗口内
                    return True
                else:
                    # 超时未检测到黄色区域，清除锁存
                    self.locked_yellow_span = None
            return False


# ==========================================
# 6. 输入控制器
# ==========================================
class InputController:
    """处理底层按键模拟"""
    @staticmethod
    def press_space():
        pydirectinput.press('space')

    @staticmethod
    def press_e():
        pydirectinput.press('e')


# ==========================================
# 7. 主应用
# ==========================================
class App:
    def __init__(self):
        self.cfg = Config()
        self.win_mgr = WindowManager()
        self.input_ctrl = InputController()
        self.hwnd = None
        self.sct = mss.MSS()
        self.detector = None
        self.tracker = None
        self.current_size = None # 记录上一次的窗口客户区尺寸
        self.last_frame_time = time.perf_counter()

    def _handle_aspect_ratio_check(self, w: int, h: int):
        """处理比例检查与用户交互"""
        if w == 0 or h == 0 or self.hwnd is None: return w, h
        if abs((w / h) - self.cfg.TARGET_ASPECT_RATIO) < 0.15: return w, h
        
        print(f"[!] 检测到窗口尺寸 {w}x{h} 比例 {w/h:.2f} 非 16:9 ({w}x{h})")

        # 如果是 MuMu 模拟器等子窗口，无法直接调整大小
        if win32gui.GetParent(self.hwnd):
            print("[-] 当前为模拟器渲染子窗口，无法自动调整大小，请在模拟器内设置分辨率为 16:9 (如 1920x1080)。")
            return w, h

        choice = input("[?] 程序暂停，是否自动调整游戏窗口大小？输入y并回车以确认。").strip().lower()
        
        if choice == 'y':
            self.win_mgr.resize_window_to_16_9(self.hwnd, w, h)
            left, top, right, bottom = self.win_mgr.get_client_rect(self.hwnd)
            return right - left, bottom - top
        else:
            print("[-] 若取消调整，识别可能存在偏差。")
            return w, h
        
    def _init_components(self, w: int, h: int):
        """初始化或重置检测组件"""
        if w == 0 or h == 0: raise ValueError("获取到的窗口客户区大小为0")
        self.current_size = (w, h)
        self.detector = QTEDetector(w, h, self.cfg)
        self.tracker = QTETracker(self.cfg)

    def _show_live_preview(self, 
                           frame: np.ndarray, 
                           red_angle: Optional[float], 
                           yellow_span: Optional[Tuple[float, float]], 
                           status_msg: str, 
                           is_hit: bool, 
                           elapsed: float, 
                           cap_elapsed: float,
                           debug_red_mask=None):
        """实时屏幕捕获模式的独立预览渲染逻辑"""
        assert self.detector is not None

        vis_frame = self.detector.render_debug(frame, red_angle, yellow_span, status_msg, is_hit, debug_red_mask, True)
        
        # 计算FPS显示
        if elapsed:
            fps = 1.0 / max(elapsed, 1e-6)
            cap_elapsed_ms = cap_elapsed * 1000
            fps_text = f"FPS: {fps:.2f} | MSS: {cap_elapsed_ms:.2f}ms"
            cv2.putText(vis_frame, fps_text, (20, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        
        window_name = "Identity V QTE Auto-Handler"
        if self.cfg.PREVIEW_WINDOW_TOP_MOST:
            cv2.namedWindow(window_name)
            cv2.setWindowProperty(window_name, cv2.WND_PROP_TOPMOST, 1)
        show_frame = cv2.resize(vis_frame, (530, 192))
        cv2.imshow(window_name, show_frame)

    def _show_video_preview(self, 
                            frame: np.ndarray, 
                            red_angle: Optional[float],
                            yellow_span: Optional[Tuple[float, float]], 
                            status_msg: str, 
                            is_hit: bool, 
                            elapsed: float,
                            debug_red_mask=None):
        """视频分析模式的独立预览渲染逻辑"""
        assert self.detector is not None

        vis_frame = self.detector.render_debug(frame, red_angle, yellow_span, status_msg, is_hit, debug_red_mask, False)
        
        # 计算FPS显示
        if elapsed:
            fps = 1.0 / max(elapsed, 1e-6)
            elapsed_ms = elapsed * 1000
            fps_text = f"FPS: {fps:.2f} | Elapsed: {elapsed_ms:.2f}ms"
            cv2.putText(vis_frame, fps_text, (30, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
        
        # 视频预览窗口不设置过大，避免占满屏幕
        show_frame = cv2.resize(vis_frame, (1280, 720))
        cv2.imshow("Identity V QTE Auto-Handler", show_frame)

    def analyse_video(self, video_path: str):
        try:
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                print(f"[!] 无法打开视频: {video_path}")
                return
            
            # 读取一帧获取视频分辨率
            ret, frame = cap.read()
            if not ret: return
            h, w = frame.shape[:2]
            self._init_components(w, h)
            print("[*] QTE 视频分析已启动")

            assert self.detector is not None

            while True:
                # 在循环最开始记录当前时间
                current_time = time.perf_counter()
                elapsed = current_time - self.last_frame_time
                self.last_frame_time = current_time
                
                # 读取视频帧
                ret, frame = cap.read()
                if not ret or self.tracker is None: 
                    break

                # 检测与追踪
                red_angle, yellow_span, debug_red_mask = self.detector.process_frame(frame)
                is_hit = self.tracker.update_and_check(red_angle, yellow_span)

                # 渲染可视化
                self._show_video_preview(frame, red_angle, self.tracker.locked_yellow_span, self.tracker.status_msg, is_hit, elapsed, debug_red_mask)

                if is_hit:
                    print(">>> 触发按键: Space <<<")
                    # 延长冷却时间并暂停视频预览命中结果
                    self.tracker.last_trigger_time += self.cfg.PREVIEW_VIDEO_HIT_TIME_SEC
                    if cv2.waitKey(int(self.cfg.PREVIEW_VIDEO_HIT_TIME_SEC * 1000)) & 0xFF == ord('q'): 
                        break
                elif cv2.waitKey(1) & 0xFF == ord('q'):
                    break

        except KeyboardInterrupt:
            print("[*] 收到 Ctrl+C，程序正在退出...")
        finally:
            cap.release()
            cv2.destroyAllWindows()
            print("[*] 资源已释放！")

    def run(self):
        # 1. 管理员权限校验
        if not self.win_mgr.is_admin():
            print("[X] 错误：请右键使用管理员权限运行此脚本！")
            sys.exit(1)

        # 诊断：输出当前 DPI 缩放信息
        dpi_scale = self.win_mgr.get_dpi_scale()
        if abs(dpi_scale - 1.0) > 0.01:
            print(f"[+] 系统 DPI 缩放: {dpi_scale:.2f}x, 已启用 DPI 感知。")

        try:
            # 2. 窗口焦点等待
            self.hwnd, client_type = self.win_mgr.wait_for_focus(self.cfg.TARGET_PROCESS_NAME)

            if client_type == "EMULATOR":
                self.hwnd = self.win_mgr.get_mumu_render_hwnd(self.hwnd)

            left, top, right, bottom = self.win_mgr.get_client_rect(self.hwnd)
            w, h = self._handle_aspect_ratio_check(right - left, bottom - top)

            # 3. 获取初始尺寸并初始化
            self._init_components(w, h)
            
            print("[*] QTE 自动化已启动，按 Ctrl+C 退出...")
            
            # 4. 主循环
            while True:
                # 在循环最开始记录当前时间
                current_time = time.perf_counter()
                elapsed = current_time - self.last_frame_time
                self.last_frame_time = current_time

                # 每次循环确认焦点是否还在游戏上，避免切屏误触
                fg_hwnd = win32gui.GetForegroundWindow()
                is_focused = (fg_hwnd == self.hwnd) or (self.win_mgr.get_top_level_hwnd(self.hwnd) == fg_hwnd)
                if not is_focused:
                    time.sleep(0.2)
                    self.last_frame_time = time.perf_counter()
                    continue
                
                # 动态获取当前帧的客户区尺寸
                left, top, right, bottom = self.win_mgr.get_client_rect(self.hwnd)
                w, h = right - left, bottom - top

                if w <= 0 or h <= 0:
                    time.sleep(0.2)
                    continue

                # 5. 监听窗口尺寸变化，若改变则重置识别器和追踪器
                if self.current_size != (w, h):
                    w, h = self._handle_aspect_ratio_check(w, h)
                    self._init_components(w, h)

                if self.tracker is None or self.detector is None:
                    raise TypeError
                
                # 仅捕获ROI以提升速度
                rx, ry, rw, rh = self.detector.arc_roi
                monitor = {"top": top + ry, "left": left + rx, "width": rw, "height": rh}
                
                start_time = time.perf_counter()

                # MSS 截图
                img = np.array(self.sct.grab(monitor))
                frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                
                # 截图+转码耗时
                cap_elapsed = time.perf_counter() - start_time
                
                # 检测与追踪
                red_angle, yellow_span, debug_red_mask = self.detector.process_frame(frame, True)
                is_hit = self.tracker.update_and_check(red_angle, yellow_span, self.cfg.SYSTEM_DELAY_MS)

                if is_hit:
                    self.input_ctrl.press_space()
                    print(">>> 触发按键: Space <<<")

                # 渲染预览
                self._show_live_preview(frame, red_angle, self.tracker.locked_yellow_span, self.tracker.status_msg, is_hit, elapsed, cap_elapsed, debug_red_mask)
                
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
                    
        except KeyboardInterrupt:
            print("[*] 收到 Ctrl+C，程序正在退出...")
        except pywintypes.error as e:
            if e.args[0] == 1400:  # 1400 对应 "无效的窗口句柄"
                print("[X] 错误：窗口句柄无效，可能是窗口已关闭。")
        finally:
            self.sct.close()
            cv2.destroyAllWindows()
            print("[*] 资源已释放！")

if __name__ == "__main__":
    app = App()
    if len(sys.argv) == 1:
        app.run()
    else:
        input_file = sys.argv[1]
        app.analyse_video(input_file)