# gui/splash_screen.py
import os
import tkinter as tk
from tkinter import font as tkfont
from typing import Optional, Callable

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from gui.ui_theme import ModernTheme as Theme  


class SplashScreen(tk.Toplevel):
    def __init__(self, parent: tk.Tk, main_app_callback: Optional[Callable[[], None]] = None):
        super().__init__(parent)
        self.main_app_callback = main_app_callback
        self.parent = parent

        # این متغیر به main.py می‌فهماند که انیمیشن تمام شده است یا نه
        self.animation_finished_var = tk.BooleanVar(value=False)

        self.overrideredirect(True)
        self.attributes("-topmost", True)
        
        width, height = 700, 450
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

        self.bg_color = getattr(Theme, 'BG_APP', getattr(Theme, 'BG_COLOR', '#1e1e1e'))
        self.primary_color = getattr(Theme, 'PRIMARY', '#00ced1')
        self.primary_light = getattr(Theme, 'PRIMARY_LIGHT', '#87ceeb')
        self.success_color = getattr(Theme, 'SUCCESS_DARK', '#00ff00')

        self.canvas = tk.Canvas(self, width=width, height=height, bg=self.bg_color, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        
        self.canvas.create_rectangle(2, 2, width-2, height-2, outline=self.primary_light, width=2)

        self.main_frame = tk.Frame(self.canvas, bg=self.bg_color)
        self.main_frame.place(relx=0.5, rely=0.5, anchor="center", width=620, height=380)

        self._build_ui()
        self._init_data()

        self.counter = 0
        self.update_progress()
        self.update_feature()

    def _build_ui(self):
        self.icon_label = tk.Label(self.main_frame, bg=self.bg_color)
        self.icon_label.pack(pady=(0, 10))
        
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        eagle_path = os.path.join(base_dir, "eagle.png")
        
        if HAS_PIL and os.path.exists(eagle_path):
            try:
                img = Image.open(eagle_path)
                img = img.resize((70, 70), Image.Resampling.LANCZOS)
                self.eagle_photo = ImageTk.PhotoImage(img)
                self.icon_label.config(image=self.eagle_photo)
            except Exception:
                self._fallback_icon()
        else:
            self._fallback_icon()

        title_font = tkfont.Font(family="Segoe UI", size=24, weight="bold")
        self.title_label = tk.Label(self.main_frame, text="NEXUS TRADER", font=title_font, fg=self.primary_light, bg=self.bg_color)
        self.title_label.pack()

        sub_font = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        self.subtitle_label = tk.Label(self.main_frame, text="V3.0 ALGORITHMIC ENGINE", font=sub_font, fg=self.primary_light, bg=self.bg_color)
        self.subtitle_label.pack(pady=(0, 20))

        term_font = tkfont.Font(family="Consolas", size=9)
        self.terminal_label = tk.Label(self.main_frame, text="[SYS] Initializing core modules...", font=term_font, fg=self.primary_light, bg="#111111", padx=10, pady=5)
        self.terminal_label.pack(fill="x", pady=(0, 10))

        self.prog_canvas = tk.Canvas(self.main_frame, height=20, bg="#0d1117", highlightthickness=1, highlightbackground=self.primary_color)
        self.prog_canvas.pack(fill="x", pady=(0, 20))
        self.prog_rect = self.prog_canvas.create_rectangle(0, 0, 0, 20, fill=self.primary_light, outline="")

        self.feature_frame = tk.Frame(self.main_frame, bg="#1a2530", highlightthickness=1, highlightbackground=self.primary_light)
        self.feature_frame.pack(fill="x", pady=(0, 10))
        
        self.did_you_know = tk.Label(self.feature_frame, text="💡 DID YOU KNOW?", font=sub_font, fg=self.primary_light, bg="#1a2530")
        self.did_you_know.pack(pady=(10, 5))

        self.feature_label = tk.Label(self.feature_frame, text="Loading intelligence modules...", font=("Segoe UI", 10), fg="#e0e7ff", bg="#1a2530", wraplength=550, justify="center")
        self.feature_label.pack(pady=(0, 10))

        self.footer_label = tk.Label(self.main_frame, text="Powered by Advanced AI & Machine Learning", font=("Segoe UI", 8), fg="#5a7a8a", bg=self.bg_color)
        self.footer_label.pack(side="bottom")

    def _fallback_icon(self):
        self.icon_label.config(text="🦅", font=("Segoe UI", 45), fg=self.primary_light)

    def _init_data(self):
        self.terminal_steps = [
            "[SYS] Establishing secure connection to Exchange API...",
            "[NET] Synchronizing live market data & timeframes...",
            "[AI] Computing Volume Moving Averages (VMA)...",
            "[AI] Scanning MACD crossovers & RSI divergences...",
            "[RISK] Applying Market Cap & Liquidity filters...",
            "[RISK] Dynamic Risk Management engine activated...",
            "[CORE] Calculating Composite Strategy Scores...",
            "[GPU] Accelerating signal processing pipeline...",
            "[SYS] Rendering professional dashboard interface..."
        ]

        self.features = [
            "Our engine evaluates RSI, MACD, and Bollinger Bands simultaneously to generate highly accurate entry/exit signals.",
            "Dynamic Risk Management: In highly volatile markets, the system automatically reduces position sizes.",
            "Smart Money Detection: Advanced algorithms identify institutional accumulation patterns.",
            "Anti-Scam Protection: Tokens with extremely low market caps or known honeypot signatures are filtered out.",
            "Lightning-Fast Execution: Our architecture processes thousands of crypto assets in real-time.",
            "Multi-Strategy Fusion: Combines momentum, mean-reversion, and volume-profile strategies."
        ]
        self.feature_index = 0

    def update_feature(self):
        self.feature_label.config(text=self.features[self.feature_index])
        self.feature_index = (self.feature_index + 1) % len(self.features)
        if self.winfo_exists():
            self.after(2000, self.update_feature)

    def update_progress(self):
        self.counter += 1
        
        self.prog_canvas.update_idletasks()
        max_width = self.prog_canvas.winfo_width()
        current_width = int((self.counter / 100) * max_width)
        self.prog_canvas.coords(self.prog_rect, 0, 0, current_width, 20)

        step_idx = int((self.counter / 100) * len(self.terminal_steps))
        if step_idx < len(self.terminal_steps):
            self.terminal_label.config(text=self.terminal_steps[step_idx])

        if self.counter < 100:
            if self.winfo_exists():
                self.after(45, self.update_progress) # سرعت تنظیم شده برای حدود 4.5 ثانیه
        else:
            self.terminal_label.config(
                text="[SYS] ✓ Launching Workspace...", 
                fg=self.success_color, 
                bg="#0a2a1a"
            )
            # اینجا 1.5 ثانیه مکث میکنیم تا کاربر موفقیت را ببیند، سپس سیگنال اتمام میدهیم
            self.after(1500, lambda: self.animation_finished_var.set(True))

    def close(self):
        if self.winfo_exists():
            self.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    root.withdraw()
    splash = SplashScreen(root)
    # تست حالت انتظار
    root.wait_variable(splash.animation_finished_var)
    splash.close()
    root.destroy()