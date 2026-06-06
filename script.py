"""
Identity V QTE Algorithm Sandbox (Video Analysis Edition)
=========================================================
专注算法验证的极简沙盒。
架构：Reader (按真实时间推流) -> Processor (极坐标降维+状态机) -> Executor (击打)
Viewer (主线程)：实时渲染 原始画面 + 极坐标展开 + 1D信号曲线
"""

import math

import cv2
import numpy as np
import time
import sys
import queue
import threading
import random
from dataclasses import dataclass, field
from collections import deque
from typing import Optional, Tuple

# ==========================================
# 1. 配置中心 (基于 1920x1080 绝对坐标)
# ==========================================
@dataclass
class Config:
    # 1080P 绝对几何参数
    ARC_CENTER_X: int = 967
    ARC_CENTER_Y: int = 900
    RADIUS: int = 338
    THICKNESS: int = 22
    START_ANGLE: int = 200
    END_ANGLE: int = 340
    
    # HSV 范围 (原样保留)
    RED_L1: np.ndarray = field(default_factory=lambda: np.array([0, 157, 90], dtype=np.uint8))
    RED_U1: np.ndarray = field(default_factory=lambda: np.array([8, 255, 255], dtype=np.uint8))
    RED_L2: np.ndarray = field(default_factory=lambda: np.array([175, 157, 90], dtype=np.uint8))
    RED_U2: np.ndarray = field(default_factory=lambda: np.array([180, 255, 255], dtype=np.uint8))
    
    YELLOW_L: np.ndarray = field(default_factory=lambda: np.array([14, 70, 140], dtype=np.uint8))
    YELLOW_U: np.ndarray = field(default_factory=lambda: np.array([23, 140, 255], dtype=np.uint8))

    # 状态机与鲁棒性参数
    COOLDOWN: float = 1.5           # 击打冷却 (秒)
    NEW_RED_MAX_ANGLE: float = 215.0# 首次出现最大角度 (防红衣误判)
    MIN_SPEED: float = 60.0         # 最小角速度 (度/秒)
    RED_WINDOW: float = 0.4         # 速度计算时间窗口 (秒)
    
    MIN_Y_SPAN: float = 4.0         # 黄色最小跨度 (度)
    MAX_Y_SPAN: float = 10.0        # 黄色最大跨度 (度)
    Y_LAG: float = 0.4              # 黄色锁存滞后时间 (秒)
    Y_TOL: float = 0.5              # 黄色稳定容差 (度，1D信号下建议稍微放宽至0.5)
    
    DELAY_COMP: float = 0.025       # 延迟补偿 (秒)

# ==========================================
# 2. 数据结构定义
# ==========================================
@dataclass
class DetectorResult:
    red_angle: Optional[float]
    yellow_span: Optional[Tuple[float, float]]
    polar_orig: np.ndarray
    polar_r: np.ndarray
    polar_y: np.ndarray
    profile_r: np.ndarray
    profile_y: np.ndarray

@dataclass
class DebugData:
    frame: np.ndarray
    red_angle: Optional[float]
    yellow_span: Optional[Tuple[float, float]]
    polar_orig: np.ndarray
    locked_y: Optional[Tuple[float, float]]
    status: str
    polar_r: np.ndarray
    polar_y: np.ndarray
    profile_r: np.ndarray
    profile_y: np.ndarray
    v_time: float


# ==========================================
# 3. 视觉检测器 (极坐标降维 2D -> 1D)
# ==========================================
class QTEDetector:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.center = (cfg.ARC_CENTER_X, cfg.ARC_CENTER_Y)
        self.radius = cfg.RADIUS
        self.thickness = cfg.THICKNESS
        
        # 计算正方形 ROI (确保圆心在中心)
        sq_half = self.radius + 10
        self.x1 = max(0, self.center[0] - sq_half)
        self.y1 = max(0, self.center[1] - sq_half)
        self.x2 = min(1920, self.center[0] + sq_half)
        self.y2 = min(1080, self.center[1] + sq_half)
        
        self.local_center = (self.center[0] - self.x1, self.center[1] - self.y1)
        
        # 极坐标展开参数
        # OpenCV warpPolar 映射规则：X轴(cols)=半径，Y轴(rows)=角度
        self.polar_width = self.radius 
        self.polar_height = 720         # 360度 * 2像素/度
        self.angle_res = self.polar_height / 360.0
        
        # 有效角度掩膜 (屏蔽 0~200 和 340~360)
        self.valid_mask = np.zeros(self.polar_height, dtype=bool)
        start_idx = int(cfg.START_ANGLE * self.angle_res)
        end_idx = int(cfg.END_ANGLE * self.angle_res)
        self.valid_mask[start_idx:end_idx] = True

    def process(self, frame_1080p: np.ndarray) -> DetectorResult:
        # 1. 裁剪 ROI
        roi = frame_1080p[self.y1:self.y2, self.x1:self.x2]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

        # 2. HSV 阈值提取
        mask_r = cv2.bitwise_or(
            cv2.inRange(hsv, self.cfg.RED_L1, self.cfg.RED_U1),
            cv2.inRange(hsv, self.cfg.RED_L2, self.cfg.RED_U2)
        )
        mask_y = cv2.inRange(hsv, self.cfg.YELLOW_L, self.cfg.YELLOW_U)
        
        # 3. 极坐标变换
        # dsize=(width, height) -> (cols, rows) -> (Radius, Angle)
        flags = cv2.WARP_POLAR_LINEAR + cv2.INTER_LINEAR
        # 对原始 ROI 画面也进行极坐标变换，作为可视化调试背景
        polar_orig = cv2.warpPolar(roi, (self.polar_width, self.polar_height), self.local_center, self.radius, flags)
        polar_r = cv2.warpPolar(mask_r, (self.polar_width, self.polar_height), self.local_center, self.radius, flags)
        polar_y = cv2.warpPolar(mask_y, (self.polar_width, self.polar_height), self.local_center, self.radius, flags)
        
        # 4. 1D 投影降维 (仅取圆环部分，排除圆心噪点)
        inner_r = max(0, self.radius - self.thickness - 5)
        # X轴是半径，Y轴是角度。
        # 切片 X轴 (半径) 从 inner_r 到 self.radius，保留所有 Y轴 (角度)。
        # 沿着 X轴 (axis=1) 求和，得到每个角度的信号强度 (1D Array length = 720)
        profile_r = np.sum(polar_r[:, inner_r:self.radius], axis=1)
        profile_y = np.sum(polar_y[:, inner_r:self.radius], axis=1)
        
        # 屏蔽无效角度
        profile_r[~self.valid_mask] = 0
        profile_y[~self.valid_mask] = 0
        
        # 5. 特征提取
        red_angle = self._get_red(profile_r, inner_r)
        yellow_span = self._get_yellow(profile_y, inner_r)
        
        return DetectorResult(red_angle, yellow_span, polar_orig, polar_r, polar_y, profile_r, profile_y)

    def _get_red(self, profile: np.ndarray, inner_r: int) -> Optional[float]:
        radial_thick = self.radius - inner_r
        # 要求指针在至少 30% 的径向厚度上贯穿 (天然抗断裂)
        threshold = radial_thick * 255 * 0.3 
        valid_idx = np.where(profile > threshold)[0]
        if len(valid_idx) == 0: return None
        
        # 顺时针旋转 -> X轴从左向右 -> 前沿为最大索引
        max_idx = np.max(valid_idx)
        return max_idx / self.angle_res

    def _get_yellow(self, profile: np.ndarray, inner_r: int) -> Optional[Tuple[float, float]]:
        radial_thick = self.radius - inner_r
        threshold = radial_thick * 255 * 0.2
        valid_idx = np.where(profile > threshold)[0]
        if len(valid_idx) == 0: return None
        
        # 寻找最长连续区间 (连通域分析)
        diffs = np.diff(valid_idx)
        splits = np.where(diffs > 1)[0] + 1
        segments = np.split(valid_idx, splits)
        best_seg = max(segments, key=len)
        
        span_deg = (best_seg[-1] - best_seg[0]) / self.angle_res
        if not (self.cfg.MIN_Y_SPAN <= span_deg <= self.cfg.MAX_Y_SPAN):
            return None
            
        return (best_seg[0] / self.angle_res, best_seg[-1] / self.angle_res)

# ==========================================
# 4. 状态追踪器 (运动预测与锁存)
# ==========================================
class QTETracker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.red_history = deque() # (angle, real_time)
        self.yellow_history = deque() # (span, real_time)
        self.locked_y = None
        self.angular_speed = 0.0
        self.last_hit_time = -999.0

    def update(self, red_angle: Optional[float], yellow_span: Optional[Tuple[float, float]], current_time: float) -> Tuple[Optional[float], str]:
        if current_time - self.last_hit_time < self.cfg.COOLDOWN:
            return None, "Cooldown"

        if red_angle is None:
            self.red_history.clear()
            if self.yellow_history and current_time - self.yellow_history[-1][1] > self.cfg.Y_LAG:
                self.yellow_history.clear()
                self.locked_y = None
            return None, "No Red"

        if not self.red_history and red_angle >= self.cfg.NEW_RED_MAX_ANGLE:
            return None, "Red Not Left"

        if not self.red_history or abs(red_angle - self.red_history[-1][0]) > 0.1:
            self.red_history.append((red_angle, current_time))
            
        while self.red_history and current_time - self.red_history[0][1] > self.cfg.RED_WINDOW:
            self.red_history.popleft()
            
        if len(self.red_history) >= 3:
            times = np.array([t for _, t in self.red_history])
            angles = np.array([a for a, _ in self.red_history])
            try:
                speed, _ = np.polyfit(times - times[0], angles, 1)
                if speed < self.cfg.MIN_SPEED: return None, "Too Slow"
                self.angular_speed = speed
            except: return None, "Speed Calc Fail"
        else:
            return None, "Wait Speed"

        if yellow_span:
            self.yellow_history.append((yellow_span, current_time))
            if len(self.yellow_history) >= 2:
                starts = [s[0][0] for s in self.yellow_history]
                ends = [s[0][1] for s in self.yellow_history]
                if max(starts)-min(starts) <= self.cfg.Y_TOL and max(ends)-min(ends) <= self.cfg.Y_TOL:
                    span_w = yellow_span[1] - yellow_span[0]
                    if self.cfg.MIN_Y_SPAN <= span_w <= self.cfg.MAX_Y_SPAN:
                        self.locked_y = yellow_span
        else:
            if self.locked_y and (not self.yellow_history or current_time - self.yellow_history[-1][1] > self.cfg.Y_LAG):
                self.locked_y = None
                
        while self.yellow_history and self.yellow_history[0][1] < current_time - self.cfg.Y_LAG:
            self.yellow_history.popleft()

        if not self.locked_y: return None, "No Yellow"

        target_angle = self.locked_y[0] + (self.locked_y[1] - self.locked_y[0]) / 3.0
        time_to_target = (target_angle - red_angle) / self.angular_speed
        hit_time = current_time + time_to_target - self.cfg.DELAY_COMP
        
        if hit_time <= current_time:
            self.last_hit_time = current_time
            return hit_time, "HIT"
            
        return hit_time, f"Approach R:{red_angle:.1f} T:{target_angle:.1f}"

# ==========================================
# 5. 多线程定义
# ==========================================
def reader_thread(video_path: str, frame_queue: queue.Queue, stop_event: threading.Event):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print("[X] 无法打开视频")
        stop_event.set()
        return
        
    start_real = time.perf_counter()
    start_video = 0.0
    
    while not stop_event.is_set():
        ret, frame = cap.read()
        if not ret: break
        
        # 强制 Resize 到 1080P
        frame = cv2.resize(frame, (1920, 1080))
        current_video_time = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        
        # 模拟真实时间流逝 (1.0x 播放速度)
        target_real = start_real + (current_video_time - start_video)
        sleep_t = target_real - time.perf_counter()
        if sleep_t > 0: time.sleep(sleep_t)
            
        # 放入队列，如果满了就丢弃最旧的 (模拟高负载下的画面延迟/丢帧)
        try:
            frame_queue.put_nowait((frame, current_video_time, time.perf_counter()))
        except queue.Full:
            try:
                frame_queue.get_nowait() 
                frame_queue.put_nowait((frame, current_video_time, time.perf_counter()))
            except queue.Empty: pass
            
    cap.release()
    stop_event.set()

def processor_thread(frame_queue: queue.Queue, action_queue: queue.Queue, render_queue: queue.Queue, stop_event: threading.Event, simulate_lag: bool):
    cfg = Config()
    detector = QTEDetector(cfg)
    tracker = QTETracker(cfg)
    
    while not stop_event.is_set():
        try:
            frame, v_time, r_time = frame_queue.get(timeout=0.1)
        except queue.Empty:
            continue
            
        res = detector.process(frame)
        hit_time, status = tracker.update(res.red_angle, res.yellow_span, r_time)
        
        if status == "HIT":
            action_queue.put((hit_time, v_time, r_time))
            print(f"[{v_time:7.3f}s] 🎯 PREDICTED HIT (Status: {status})")

        debug_data = DebugData(
            frame=frame, red_angle=res.red_angle, yellow_span=res.yellow_span,
            polar_orig=res.polar_orig, locked_y=tracker.locked_y, status=status, polar_r=res.polar_r,
            polar_y=res.polar_y, profile_r=res.profile_r, profile_y=res.profile_y, v_time=v_time
        )

        try:
            render_queue.put_nowait(debug_data)
        except queue.Full:
            try: render_queue.get_nowait()
            except queue.Empty: pass
            render_queue.put_nowait(debug_data)
            
        # 模拟低帧率/高负载下的处理耗时 (验证算法鲁棒性)
        if simulate_lag:
            time.sleep(random.uniform(0.015, 0.045)) 

def executor_thread(action_queue: queue.Queue, stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            hit_time, v_time, r_time = action_queue.get(timeout=0.1)
            delay = hit_time - time.perf_counter()
            if delay > 0: time.sleep(delay)
            
            # 计算从“看到画面”到“实际击打”的真实反应延迟
            react_ms = (time.perf_counter() - r_time) * 1000
            print(f"[{v_time:7.3f}s] 🔥 EXECUTE SPACE! (React Delay: {react_ms:.1f}ms)")
        except queue.Empty:
            continue

# ==========================================
# 6. 主程序 (包含可视化渲染循环)
# ==========================================
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python script.py <video_path> [--lag]")
        sys.exit(1)
        
    video_file = sys.argv[1]
    enable_lag = "--lag" in sys.argv
    
    fq = queue.Queue(maxsize=5) # 限制队列大小，强制触发丢帧逻辑
    aq = queue.Queue()
    rq = queue.Queue(maxsize=2)
    stop = threading.Event()
    
    print(f"[*] 启动分析: {video_file}")
    if enable_lag: print("[!] 已开启模拟延迟 (--lag)，测试鲁棒性...")
    
    t1 = threading.Thread(target=reader_thread, args=(video_file, fq, stop))
    t2 = threading.Thread(target=processor_thread, args=(fq, aq, rq, stop, enable_lag))
    t3 = threading.Thread(target=executor_thread, args=(aq, stop))
    
    t1.start(); t2.start(); t3.start()

    # --- 主线程作为 Viewer ---
    cfg = Config()
    detector = QTEDetector(cfg) # 仅用于获取参数
    
    cv2.namedWindow("Original", cv2.WINDOW_NORMAL)
    cv2.namedWindow("Polar", cv2.WINDOW_NORMAL)
    cv2.namedWindow("Profiles", cv2.WINDOW_NORMAL)
    
    try:
        while not stop.is_set():
            try:
                data: DebugData = rq.get(timeout=0.1)
            except queue.Empty:
                if cv2.waitKey(1) & 0xFF == ord('q'): stop.set(); break
                continue
                
            # 1. 绘制原始画面
            scale = 0.5
            vis_orig = cv2.resize(data.frame, (960, 540))
            # 绘制圆环区域
            cx, cy = int(cfg.ARC_CENTER_X * scale), int(cfg.ARC_CENTER_Y * scale)
            radius = int(cfg.RADIUS * scale)
            inner_r = int((cfg.RADIUS - cfg.THICKNESS) * scale)
            cv2.ellipse(vis_orig, (cx, cy), (radius, radius), 0, cfg.START_ANGLE, cfg.END_ANGLE, (255, 255, 255), 1)
            cv2.ellipse(vis_orig, (cx, cy), (inner_r, inner_r), 0, cfg.START_ANGLE, cfg.END_ANGLE, (255, 255, 255), 1)
            # 绘制检测结果
            if data.locked_y:
                for ang in data.locked_y:
                    rad = math.radians(ang)
                    ex, ey = int(cx + radius * math.cos(rad)), int(cy + radius * math.sin(rad))
                    cv2.line(vis_orig, (cx, cy), (ex, ey), (0, 255, 255), 2)
            if data.red_angle is not None:
                rad = math.radians(data.red_angle)
                ex, ey = int(cx + radius * math.cos(rad)), int(cy + radius * math.sin(rad))
                cv2.circle(vis_orig, (ex, ey), 4, (0, 0, 255), -1)
                if data.status != "Red Not Left":
                    cv2.line(vis_orig, (cx, cy), (ex, ey), (0, 0, 255), 2)
            # 绘制状态文本
            color = (0, 255, 0) if "HIT" in data.status else (255, 255, 255)
            cv2.putText(vis_orig, data.status, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2, color, 3)
            cv2.putText(vis_orig, f"Time: {data.v_time:.2f}s", (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2)
            
            # 2. 绘制极坐标展开图
            h_p, w_p = data.polar_r.shape[:2] # h_p = Angle, w_p = Radius
            # 以原图极坐标展开为背景
            vis_polar = data.polar_orig.copy()
            # 绘制掩膜颜色 (BGR格式)
            vis_polar[data.polar_r > 0] = [0, 0, 255]     # 红色
            vis_polar[data.polar_y > 0] = [0, 255, 255]   # 黄色
            # 保留 180~360度 (对应行 360~720)，即转置后图像的右半部分
            vis_polar = vis_polar[360:720, :, :]
            # 转置矩阵，使得 X轴 = 角度(720), Y轴 = 半径(w_p)
            # 原始 shape: (Angle, Radius, 3) -> 转置后: (Radius, Angle, 3)
            # 在图像中，Height = Radius, Width = Angle
            vis_polar_t = vis_polar.transpose(1, 0, 2)
            vis_polar_resized = cv2.resize(vis_polar_t, (960, 340), interpolation=cv2.INTER_NEAREST)

            # inner_r 在 Y轴 (半径) 上的位置
            y_line = int((cfg.RADIUS - cfg.THICKNESS - 5) * (340 / w_p))
            cv2.line(vis_polar_resized, (0, y_line), (960, y_line), (255, 255, 255), 1) # 标记 inner_r
            
            # 3. 绘制 1D 投影曲线
            plot_h, plot_w = 200, 960
            vis_plot = np.zeros((plot_h, plot_w, 3), dtype=np.uint8)
            
            # 只保留 200~340度 的范围作为关注点
            # 200度 -> 索引 400, 340度 -> 索引 680 (因为 angle_res = 2.0)
            start_idx_crop = int(200 * detector.angle_res)
            end_idx_crop = int(340 * detector.angle_res)
            
            profile_r_crop = data.profile_r[start_idx_crop:end_idx_crop]
            profile_y_crop = data.profile_y[start_idx_crop:end_idx_crop]
            
            max_val = max(np.max(profile_r_crop), np.max(profile_y_crop), 1)
            h_r = (profile_r_crop / max_val * (plot_h - 20)).astype(int)
            h_y = (profile_y_crop / max_val * (plot_h - 20)).astype(int)

            # 将裁剪后的 1D 信号 (长度 280) 插值拉伸到 plot_w (960)
            x_old = np.arange(len(h_r))
            x_new = np.linspace(0, len(h_r) - 1, plot_w)
            norm_r = np.interp(x_new, x_old, h_r).astype(int)
            norm_y = np.interp(x_new, x_old, h_y).astype(int)
            
            pts_r = np.column_stack((np.arange(plot_w), plot_h - norm_r)).astype(np.int32)
            pts_y = np.column_stack((np.arange(plot_w), plot_h - norm_y)).astype(np.int32)
            
            cv2.polylines(vis_plot, [pts_r], False, (0, 0, 255), 2)
            cv2.polylines(vis_plot, [pts_y], False, (0, 255, 255), 2)
            
            # 4. 多窗口独立显示
            cv2.imshow("Original", vis_orig)
            cv2.imshow("Polar", vis_polar_resized)
            cv2.imshow("Profiles", vis_plot)
            
            if cv2.waitKey(1) & 0xFF == ord('q'):
                stop.set()
                break
    except KeyboardInterrupt:
        print("[*] 手动终止...")
    finally:
        stop.set()
        t1.join(); t2.join(); t3.join()
        print("[*] 分析结束。")