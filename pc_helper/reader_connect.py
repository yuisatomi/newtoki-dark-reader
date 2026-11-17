"""Double-click Windows helper; credentials stay in memory and travel over SSH."""
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, ttk
import webbrowser

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import reader_openai

SERVER = 'root@192.168.100.118'
APP_URL = 'https://reader-sync.flolim.com/app'


class ConnectionWindow:
    def __init__(self, root, automatic=True):
        self.root, self.events = root, queue.Queue()
        self.running, self.finishing, self.closing = False, False, False
        self.cancel = threading.Event()
        root.title('읽기 뷰어 · ChatGPT 연결')
        root.geometry('650x365')
        root.minsize(650, 365)
        root.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(root)
        style.configure('TLabel', font=('맑은 고딕', 10))
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='ChatGPT를 읽기 뷰어에 연결', font=('맑은 고딕', 16, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='기본 브라우저에서 로그인하고 사용 권한을 승인하세요.\n한 번 연결하면 모든 기기의 뷰어가 같은 연결을 사용합니다.', wraplength=590).pack(anchor='w', pady=(10, 12))
        ttk.Label(frame, text='서버: 192.168.100.118 · 사내망 또는 NetBird 연결 필요').pack(anchor='w')
        key_row = ttk.Frame(frame)
        key_row.pack(fill='x', pady=(8, 5))
        self.key = tk.StringVar(value=str(Path.home() / '.ssh' / 'reader-lxc108_ed25519'))
        self.key_entry = ttk.Entry(key_row, textvariable=self.key, state='readonly')
        self.key_entry.pack(side='left', fill='x', expand=True)
        self.choose = ttk.Button(key_row, text='SSH 키 선택', command=self.choose_key)
        self.choose.pack(side='left', padx=(8, 0))
        self.new_account = tk.BooleanVar(value=False)
        self.account_option = ttk.Checkbutton(frame, text='기존 연결과 다른 ChatGPT 계정으로 연결', variable=self.new_account)
        self.account_option.pack(anchor='w')
        self.status = tk.StringVar(value='연결을 준비하고 있습니다…')
        tk.Label(frame, textvariable=self.status, wraplength=590, height=4, anchor='nw', justify='left',
                 font=('맑은 고딕', 10), background=style.lookup('TFrame', 'background') or root.cget('background')).pack(fill='x', pady=(16, 8))
        self.progress = ttk.Progressbar(frame, mode='indeterminate')
        self.progress.pack(fill='x')
        actions = ttk.Frame(frame)
        actions.pack(fill='x', pady=(14, 0))
        self.connect_button = ttk.Button(actions, text='ChatGPT 연결', command=self.start)
        self.connect_button.pack(side='left')
        self.viewer_button = ttk.Button(actions, text='뷰어 열기', command=lambda: webbrowser.open(APP_URL))
        self.viewer_button.pack(side='left', padx=8)
        self.close_button = ttk.Button(actions, text='닫기', command=self.close)
        self.close_button.pack(side='right')
        root.update_idletasks()
        width, height = max(650, root.winfo_reqwidth()), max(365, root.winfo_reqheight())
        root.geometry(f'{width}x{height}')
        root.minsize(width, height)
        self.timer = root.after(100, self.drain)
        self.start_timer = root.after(350, self.start) if automatic else None

    def choose_key(self):
        path = filedialog.askopenfilename(parent=self.root, title='서버 접속용 SSH 개인키 선택', initialdir=Path.home() / '.ssh')
        if path:
            self.key.set(path)

    def start(self):
        if self.running:
            return
        if not Path(self.key.get()).is_file() or self.key.get().lower().endswith('.pub'):
            self.status.set('이 PC에 서버 접속 키가 없습니다. 「SSH 키 선택」에서 등록된 개인키를 선택하세요.')
            return
        self.running, self.finishing, self.closing = True, False, False
        self.cancel.clear()
        self.progress.start(12)
        for widget in (self.connect_button, self.choose, self.account_option):
            widget.configure(state='disabled')
        self.close_button.configure(text='취소 · 닫기')
        key, new_account = self.key.get(), self.new_account.get()

        def report(text):
            if text.startswith('서버에 등록하고'):
                self.finishing = True
            self.events.put(('progress', text))

        def work():
            try:
                status = reader_openai.connect_ssh(SERVER, key, new_account,
                    progress=report, cancel=self.cancel)
                self.events.put(('success', status))
            except reader_openai.OpenAIError as error:
                self.events.put(('error', str(error)))
            except Exception:
                self.events.put(('error', '연결 도우미 실행에 실패했습니다. 네트워크와 SSH 설정을 확인한 뒤 다시 시도하세요.'))
        threading.Thread(target=work, name='reader-chatgpt-connect', daemon=True).start()

    def drain(self):
        self.timer = None
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'progress':
                    self.status.set(value)
                    if self.finishing:
                        self.close_button.configure(state='disabled', text='마무리 중…')
                    continue
                self.running = False
                self.progress.stop()
                self.close_button.configure(state='normal', text='닫기')
                for widget in (self.connect_button, self.choose, self.account_option):
                    widget.configure(state='normal')
                if kind == 'success':
                    email = str(value.get('email', '연결된 계정'))
                    self.status.set(f'연결 완료 · {email}\nGPT-6 Luna 사용 가능. 뷰어의 연결 상태를 새로고침하세요.')
                else:
                    self.status.set(value)
                if self.closing:
                    self.destroy()
                    return
        except queue.Empty:
            pass
        self.timer = self.root.after(100, self.drain)

    def close(self):
        if self.running:
            if not self.finishing:
                self.closing = True
                self.cancel.set()
                self.status.set('연결을 취소하고 있습니다. 잠시만 기다려주세요…')
                self.close_button.configure(state='disabled')
            return
        self.destroy()

    def destroy(self):
        for timer in (self.timer, self.start_timer):
            if timer:
                self.root.after_cancel(timer)
        self.root.destroy()


def main():
    root = tk.Tk()
    if '--self-test' in sys.argv:
        root.withdraw()
        window = ConnectionWindow(root, automatic=False)
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        assert jwt.decode(jwt.encode({'sub': 'bundle-check'}, key, algorithm='RS256'), key.public_key(), algorithms=['RS256'])['sub'] == 'bundle-check'
        window.events.put(('success', {'email': 'bundle-check@example.invalid'}))
        root.after(150, root.quit)
        root.mainloop()
        assert '연결 완료' in window.status.get()
        window.destroy()
        return
    ConnectionWindow(root)
    root.mainloop()


if __name__ == '__main__':
    main()
