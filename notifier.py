# notifier.py  —  Windows 原生 Toast 通知（best-effort，绝不影响主流程）
"""
目标：Agent 需要人工审批、连续失败暂停、任务结束这三类事件发生时，
在用户桌面弹一条 Windows 10/11 通知，把"切窗回来确认"变成可选动作。

设计要点：
- 桥接方式是系统自带的 Windows PowerShell 5.1（powershell.exe）。
  WinRT 投影 [Windows.UI.Notifications.ToastNotificationManager] 只在
  PowerShell 5.1 可用，pwsh 7 不行，所以这里绝不用 pwsh。
- 参数数组 + shell=False，绝不把 title/message 交给 shell 二次解释。
- fire-and-forget：stdin/stdout/stderr 全部 DEVNULL，且不 wait——
  powershell 冷启动要 1~2 秒，交互循环不能被它卡住。
- 任何失败路径（进程不存在、启动异常、XML 组装异常）都吞掉返回 False，
  不向上抛；通知是纯增强，坏了也不许影响审批/执行语义。
- TTY 守卫：非交互环境（测试、CI、管道捕获输出）直接 no-op，
  避免跑测试时在用户桌面狂弹通知；force=True 跳过全部守卫，供冒烟/测试用。

AUMID 自愈：
- Windows 只认"注册过的应用身份"（AUMID）。硬编码的 PowerShell AUMID 在
  未注册的机器上 toast 会被无声拒收（0x80073D54 没有包标识）。
- resolve_aumid() 进程内缓存一次：优先用自注册的 HQin 专用 AUMID
  （开始菜单 .lnk 存在即生效），否则用 Get-StartApps 枚举本机已注册
  AUMID，经 pick_aumid() 按优先级挑一个；枚举失败回落硬编码默认值。

诊断入口（python -m notifier）：
- python -m notifier "标题" "正文"     同步发送一条测试 toast，打印
  PowerShell 报错原文 + 只读注册表检查（总开关/应用级开关/勿扰）+ 中文建议。
- python -m notifier --register        自注册 HQin 专用 AUMID（写开始菜单
  .lnk + AppUserModelId 注册表，Add-Type C# IShellLink/IPropertyStore）。
"""
import os
import re
import subprocess
import sys

import config

# ──────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────

# 默认 AUMID：系统自带 Windows PowerShell 的注册项（BurntToast / winotify
# 同款默认值）。仅在枚举失败/未自注册时回落使用。
DEFAULT_AUMID = r"{1AC14E86-1E54-5FA7-B6C6-A3C4E4A5123B}\WindowsPowerShell\v1.0"

# 自注册专用 AUMID（--register 写入开始菜单 .lnk 后由 notify 优先使用）。
SELF_AUMID = "HQin.Harness.Notifier"
_LNK_NAME = "HQin Harness Notifier.lnk"

# 显式指向 System32 的 Windows PowerShell 5.1，不受 PATH 上 pwsh 7 干扰。
_SYSTEM_ROOT = os.environ.get("SystemRoot", r"C:\Windows")
_POWERSHELL = _SYSTEM_ROOT + r"\System32\WindowsPowerShell\v1.0\powershell.exe"

_TITLE_LIMIT = 200
_MESSAGE_LIMIT = 500

# XML 文本节点必须转义的五个字符：& < > " '
# 实体写成 chr(38)+后缀，避免源码里再出现一层转义歧义。
_AMP = chr(38)
_XML_ESCAPES = [
    (_AMP, _AMP + "amp;"),
    ("<", _AMP + "lt;"),
    (">", _AMP + "gt;"),
    ('"', _AMP + "quot;"),
    ("'", _AMP + "apos;"),
]

# 换行压成空格：toast 正文保持单行，也保证 here-string 的 '@ 终止符
# 永远不会出现在行首。
_WS_RE = re.compile(r"[\r\n\t]+")


# ──────────────────────────────────────────────
# XML 与脚本组装
# ──────────────────────────────────────────────

def _xml_escape(text: str) -> str:
    """转义 toast XML 文本节点中的五个 XML 特殊字符。"""
    for raw, escaped in _XML_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def _creation_flags() -> int:
    """
    返回 Popen 的 creationflags。

    正常终端（stdout 是 TTY）里用 CREATE_NO_WINDOW 隐藏 powershell 的控制台
    子进程，避免 Windows 上一闪而过的黑框。无控制台环境（DSH 沙盒、GUI
    宿主）里省略该 flag：实测沙盒的无控制台 worker 下，带 CREATE_NO_WINDOW
    的子进程会以 0xC0000142（DLL 初始化失败）退出，省略后子进程继承宿主
    控制台即可正常运行。非 Windows 平台返回 0。
    """
    if sys.platform != "win32":
        return 0
    try:
        interactive = bool(sys.stdout and sys.stdout.isatty())
    except Exception:
        interactive = False
    return subprocess.CREATE_NO_WINDOW if interactive else 0


def _toast_script(aumid: str, xml: str, sound_path: str | None = None) -> str:
    # WinRT 投影类型必须用 "Namespace, ContentType = WindowsRuntime" 语法显式
    # 加载（ToastNotification / XmlDocument 都要）；静态方法名是
    # CreateToastNotifier（不是 CreateToast）。XML 用单引号 here-string
    # 传入，内容零插值、零转义歧义。
    #
    # 自定义提示音：toast 自身的 <audio> 只接受系统预置音效或打包应用资源，
    # 不能指向任意文件。因此自定义 wav 时 toast 设为静音，改用 SoundPlayer
    # 在同一进程内播放（PCM .wav）；未配置声音则保持系统默认。
    if sound_path:
        xml = xml.replace(
            "<toast>",
            "<toast><audio silent='true'/>",
            1,
        )
        play = (
            "; $player = New-Object System.Media.SoundPlayer("
            + _ps_quote(sound_path)
            + "); $player.PlaySync()"
        )
    else:
        play = ""
    return (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] | Out-Null; "
        "[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] | Out-Null; "
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, "
        "ContentType = WindowsRuntime] | Out-Null; "
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
        f"$xml.LoadXml(@'\n{xml}\n'@); "
        "$toast = New-Object Windows.UI.Notifications.ToastNotification($xml); "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        f"'{aumid}').Show($toast)"
        + play
    )


def _resolve_sound() -> str | None:
    """
    解析自定义提示音路径（每次 notify 调用时读取，便于运行中改配置）。
    空/文件不存在/非文件 → None（退回系统默认声音）；路径存在则原样返回。
    校验全部在 Python 侧完成，PowerShell 只收到单引号转义的字面量。
    """
    path = (getattr(config, "NOTIFY_SOUND", "") or "").strip()
    if not path:
        return None
    if not os.path.isfile(path):
        return None
    return path


def build_ps_args(title: str, message: str, aumid: str | None = None,
                  sound_path: str | None = None) -> list[str]:
    """
    组装 powershell.exe 的参数数组（纯函数，便于无进程单测）。

    title 截断到 200 字符、message 截断到 500 字符后做 XML 转义，
    嵌入 ToastGeneric 模板（title + body 两行文本）。
    截断在转义之前做，保证转义后的合法实体不会被拦腰截断。
    aumid 为 None 时使用 DEFAULT_AUMID，保持本函数零副作用。
    sound_path 为自定义 PCM wav 的绝对路径：toast 静音 + SoundPlayer 播放。
    """
    title = _WS_RE.sub(" ", (title or ""))[:_TITLE_LIMIT]
    message = _WS_RE.sub(" ", (message or ""))[:_MESSAGE_LIMIT]
    xml = (
        "<toast><visual><binding template='ToastGeneric'>"
        f"<text id='1'>{_xml_escape(title)}</text>"
        f"<text id='2'>{_xml_escape(message)}</text>"
        "</binding></visual></toast>"
    )
    return [
        _POWERSHELL,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        _toast_script(aumid or DEFAULT_AUMID, xml, sound_path),
    ]


# ──────────────────────────────────────────────
# AUMID 解析（自愈）
# ──────────────────────────────────────────────

def pick_aumid(registered: list[str]) -> str:
    """
    纯函数：从已注册 AUMID 列表里按优先级挑一个；列表为空回落默认值。

    优先级：含 "WindowsPowerShell" → 含 "Microsoft.PowerShell" →
    其他 PowerShell 变体 → DEFAULT_AUMID。
    """
    cleaned = [str(a).strip() for a in (registered or []) if a and str(a).strip()]
    if not cleaned:
        return DEFAULT_AUMID
    for needle in ("WindowsPowerShell", "Microsoft.PowerShell"):
        for aumid in cleaned:
            if needle.lower() in aumid.lower():
                return aumid
    for aumid in cleaned:
        if "powershell" in aumid.lower():
            return aumid
    return DEFAULT_AUMID


def _startapps_args() -> list[str]:
    """Get-StartApps 枚举脚本的参数数组（本机已注册 AUMID 名单）。"""
    script = (
        "if (Get-Command Get-StartApps -ErrorAction SilentlyContinue) { "
        "foreach ($a in (Get-StartApps)) { if ($a.AppID) { Write-Output $a.AppID } } }"
    )
    return [_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script]


def _enumerate_aumids() -> list[str]:
    """同步跑一次 Get-StartApps，返回 AUMID 列表；任何失败返回空列表。"""
    if sys.platform != "win32":
        return []
    try:
        result = subprocess.run(
            _startapps_args(),
            shell=False,
            capture_output=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            creationflags=_creation_flags(),
        )
    except Exception:
        return []
    raw = result.stdout
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def _self_lnk_path() -> str:
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", _LNK_NAME)


_aumid_cache: str | None = None


def reset_aumid_cache() -> None:
    """清空进程内 AUMID 缓存（诊断/测试用）。"""
    global _aumid_cache
    _aumid_cache = None


def resolve_aumid() -> str:
    """
    解析本次进程应使用的 AUMID（进程内缓存一次，之后零开销）。

    优先级：自注册 .lnk 存在 → SELF_AUMID；否则 Get-StartApps 枚举 +
    pick_aumid 挑选；枚举失败回落 DEFAULT_AUMID。
    枚举本身会同步 spawn 一次 powershell（首次 notify / 诊断时），上限 30s。
    """
    global _aumid_cache
    if _aumid_cache is None:
        try:
            if os.path.isfile(_self_lnk_path()):
                _aumid_cache = SELF_AUMID
            else:
                _aumid_cache = pick_aumid(_enumerate_aumids())
        except Exception:
            _aumid_cache = DEFAULT_AUMID
    return _aumid_cache


# ──────────────────────────────────────────────
# 主路径（fire-and-forget）
# ──────────────────────────────────────────────

def notify(title: str, message: str, force: bool = False) -> bool:
    """
    Best-effort 弹 Windows toast；任何失败都不抛异常，失败返回 False。

    守卫（force=False 时，任一命中即 no-op 返回 False）：
    1. 非 Windows 平台；
    2. stdout 不是 TTY（测试/CI/管道捕获环境）；
    3. config.NOTIFY_ENABLED 为 False（HQIN_NOTIFY=false）。
    force=True 跳过全部守卫，供真实冒烟与单测使用。
    """
    try:
        if not force:
            if sys.platform != "win32":
                return False
            try:
                if not sys.stdout.isatty():
                    return False
            except Exception:
                return False
            if not getattr(config, "NOTIFY_ENABLED", False):
                return False
        aumid = resolve_aumid()
        subprocess.Popen(
            build_ps_args(title, message, aumid, _resolve_sound()),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_creation_flags(),
        )
        return True
    except Exception:
        return False


# ──────────────────────────────────────────────
# 只读注册表检查（诊断）
# ──────────────────────────────────────────────

def registry_verdict(values: dict) -> str:
    """
    纯函数：输入注册表读数，输出中文结论（多行字符串）。

    values 的键（值语义：True=明确开启，False=明确关闭，None=读不到/未知）：
    - toast_enabled  系统通知总开关（ToastEnabled）
    - aumid_enabled  本应用通知开关（Settings\\<AUMID>\\Enabled）
    - dnd            专注助手/勿扰模式
    注意：aumid_enabled 读不到（键不存在）通常是"未被单独关闭"，属正常；
    总开关键不存在同样是默认开启；专注助手的实时状态存 CloudStore
    二进制，注册表读不到可靠值，恒为 None（请手动检查）。
    """
    lines = []
    toast = values.get("toast_enabled")
    if toast is False:
        lines.append("✗ 系统通知总开关已关闭（设置 > 系统 > 通知，重新打开即可）")
    elif toast is True:
        lines.append("✓ 系统通知总开关开启")
    else:
        lines.append("? 系统通知总开关：未知（键不存在通常等于默认开启）")

    aumid = values.get("aumid_enabled")
    if aumid is False:
        lines.append("✗ 本应用（当前 AppId）的通知被单独关闭（设置 > 通知 里找到它打开）")
    elif aumid is True:
        lines.append("✓ 当前 AppId 的应用级通知开关开启")
    else:
        lines.append("? AppId 级开关：未知（键不存在通常等于未被单独关闭，正常）")

    dnd = values.get("dnd")
    if dnd is True:
        lines.append("✗ 专注助手/勿扰模式疑似开启，会吞掉 toast 横幅（只进通知中心）")
    elif dnd is False:
        lines.append("✓ 专注助手/勿扰模式未开启")
    else:
        lines.append("? 专注助手：无法从注册表判断，请手动检查（Win+A 面板 / 设置 > 系统 > 专注助手）")

    return "\n".join(lines)


def _reg_query(root, path: str, value: str):
    """只读查询一个注册表值；任何失败返回 None，绝不抛错。"""
    try:
        import winreg
        with winreg.OpenKey(root, path) as key:
            data, _ = winreg.QueryValueEx(key, value)
        return data
    except Exception:
        return None


def _read_registry_values(aumid: str) -> dict:
    """
    只读收集诊断所需的注册表读数；全部失败降级为 None（未知）。
    专注助手的实时状态存在 CloudStore 二进制里，注册表层面读不到可靠
    值，因此 dnd 恒为 None（结论交给 registry_verdict 的"请手动检查"）。
    """
    values = {"toast_enabled": None, "aumid_enabled": None, "dnd": None}
    if sys.platform != "win32":
        return values
    try:
        import winreg
        raw = _reg_query(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\PushNotifications",
            "ToastEnabled",
        )
        if raw is not None:
            values["toast_enabled"] = bool(int(raw) != 0)
        raw = _reg_query(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings"
            + "\\" + (aumid or ""),
            "Enabled",
        )
        if raw is not None:
            values["aumid_enabled"] = bool(int(raw) != 0)
    except Exception:
        pass
    return values


# ──────────────────────────────────────────────
# 排查建议
# ──────────────────────────────────────────────

def advise_from_error(stderr_text: str, exit_code: int, values: dict) -> list[str]:
    """
    纯函数：根据 PowerShell 报错原文与注册表读数，生成中文排查建议列表。

    识别的典型错误：
    - 0x80073D54 / "没有包标识"：AUMID 未被系统承认 → 建议 --register
    - ToastEnabled=0：总开关关闭
    - 应用级 Enabled=0：当前 AppId 被单独关闭
    - 退出码 0：脚本层面成功，问题在系统层（勿扰/通知中心/横幅被折叠）
    """
    text = stderr_text or ""
    tips: list[str] = []

    if "80073D54" in text or "包标识" in text or "package identity" in text.lower():
        tips.append(
            "当前 AppId 未被 Windows 承认（0x80073D54 没有包标识）——这是"
            " toast 不弹的最常见原因。运行下面的命令注册 HQin 专用 AppId，"
            "然后重新诊断：\n    python -m notifier --register"
        )
    if values.get("toast_enabled") is False:
        tips.append("系统通知总开关是关的：设置 > 系统 > 通知，打开「获取来自应用和其他发送者的通知」。")
    if values.get("aumid_enabled") is False:
        tips.append("当前 AppId 的通知被单独关闭：设置 > 通知，找到对应应用重新允许。")
    if not tips and exit_code == 0:
        tips.append(
            "脚本执行成功但桌面没弹？依次检查：① 专注助手/勿扰（Win+A）；"
            "② 通知横幅是否只进了通知中心（Win+N）；③ 多显示器/全屏应用压制横幅。"
        )
    if exit_code != 0 and "80073D54" not in text and "包标识" not in text:
        tips.append(
            "PowerShell 执行出错（见上方原文）。若报错提到无法加载 "
            "Windows.UI.Notifications，说明本机 Windows PowerShell 5.1 的 "
            "WinRT 投影不可用；确认 System32 下的 powershell.exe 未被移除。"
        )
    if not tips:
        tips.append("未发现明确异常。若桌面仍无 toast，检查专注助手（Win+A）与通知中心（Win+N）。")
    return tips


# ──────────────────────────────────────────────
# 自注册（--register）
# ──────────────────────────────────────────────

# Add-Type C#：IShellLinkW + IPersistFile + IPropertyStore，把
# PKEY_AppUserModel_ID 写进开始菜单 .lnk。非打包桌面应用获得 toast
# 身份的标准做法。接口按 vtable 顺序完整声明，避免布局错位。
_REGISTER_CS = r"""
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

public static class HQinShortcut {
  [ComImport, Guid("000214EE-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IShellLinkW {
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszFile, int cch, IntPtr pfd, uint fFlags);
    IntPtr GetIDList();
    void SetIDList(IntPtr pidl);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszName, int cch);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszDir, int cch);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszArgs, int cch);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);
    short GetHotKey();
    void SetHotKey(short wHotKey);
    uint GetShowCmd();
    void SetShowCmd(uint iShowCmd);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszIconPath, int cch, out int piIcon);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, uint dwReserved);
    void Resolve(IntPtr hwnd, uint fFlags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);
  }

  [ComImport, Guid("0000010B-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IPersistFile {
    void GetClassID(out Guid pClassID);
    int IsDirty();
    void Load([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, uint dwMode);
    void Save([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, bool fRemember);
    void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string pszFileName);
    void GetCurFile([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszFile);
  }

  [StructLayout(LayoutKind.Sequential)]
  public struct PROPERTYKEY { public Guid fmtid; public uint pid; }

  [StructLayout(LayoutKind.Explicit)]
  public struct PROPVARIANT {
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public IntPtr pointer;
  }

  [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IPropertyStore {
    int GetCount(out uint cProps);
    int GetAt(uint iProp, out PROPERTYKEY pkey);
    int GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
    int SetValue(ref PROPERTYKEY key, ref PROPVARIANT pv);
    int Commit();
  }

  public static void Create(string lnkPath, string targetPath, string arguments, string aumid, string description) {
    Guid clsid = new Guid("00021401-0000-0000-C000-000000000046");
    Type shellLinkType = Type.GetTypeFromCLSID(clsid);
    object obj = Activator.CreateInstance(shellLinkType);
    try {
      IShellLinkW link = (IShellLinkW)obj;
      link.SetPath(targetPath);
      if (!String.IsNullOrEmpty(arguments)) link.SetArguments(arguments);
      if (!String.IsNullOrEmpty(description)) link.SetDescription(description);
      IPersistFile persist = (IPersistFile)obj;
      persist.Save(lnkPath, false);
      IPropertyStore store = (IPropertyStore)obj;
      PROPERTYKEY key = new PROPERTYKEY();
      key.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
      key.pid = 5;
      PROPVARIANT pv = new PROPVARIANT();
      pv.vt = 31;
      pv.pointer = Marshal.StringToCoTaskMemUni(aumid);
      store.SetValue(ref key, ref pv);
      store.Commit();
      Marshal.FreeCoTaskMem(pv.pointer);
    } finally {
      Marshal.ReleaseComObject(obj);
    }
  }
}
"""


def _ps_quote(value: str) -> str:
    """转义成 PowerShell 单引号字面量（' → ''）。"""
    return "'" + (value or "").replace("'", "''") + "'"


def register_args() -> list[str]:
    """组装 --register 的 powershell 参数数组（便于离线单测）。"""
    lnk = _self_lnk_path()
    target = _POWERSHELL
    script = (
        "$code = @'\n" + _REGISTER_CS + "\n'@; "
        "Add-Type -TypeDefinition $code -Language CSharp; "
        "[HQinShortcut]::Create(" + _ps_quote(lnk) + ", " + _ps_quote(target)
        + ", '-NoProfile', " + _ps_quote(SELF_AUMID) + ", " + _ps_quote("HQin Harness 通知") + "); "
        "$k = 'HKCU:\\Software\\Classes\\AppUserModelId\\" + SELF_AUMID + "'; "
        "New-Item -Path $k -Force | Out-Null; "
        "New-ItemProperty -Path $k -Name 'DisplayName' -Value 'HQin Harness' "
        "-PropertyType String -Force | Out-Null; "
        "Write-Output REGISTER-OK"
    )
    return [_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script]


def do_register() -> tuple[bool, str]:
    """
    执行自注册（同步，best-effort）。

    成功后 notify 走 SELF_AUMID（.lnk 存在即被 resolve_aumid 命中）。
    任何失败静默返回 (False, 原因)，不向上抛。
    """
    if sys.platform != "win32":
        return False, "非 Windows 平台，无需注册。"
    try:
        result = subprocess.run(
            register_args(),
            shell=False,
            capture_output=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            creationflags=_creation_flags(),
        )
        raw = result.stdout
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if result.returncode == 0 and "REGISTER-OK" in (raw or ""):
            reset_aumid_cache()
            return True, f"已注册 AppId={SELF_AUMID}（开始菜单快捷方式 + 注册表），notify 将自动使用它。"
        err = result.stderr
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        reason = (err or "").strip() or f"exit code={result.returncode}"
        return False, "注册失败: " + reason[:400]
    except Exception as exc:
        return False, "注册失败（无法启动 powershell）: " + str(exc)[:200]


# ──────────────────────────────────────────────
# 诊断 CLI（python -m notifier）
# ──────────────────────────────────────────────

def diagnose_send(title: str, message: str) -> int:
    """
    同步发送一条测试 toast 并打印完整诊断。返回退出码（0=脚本成功）。

    与 notify() 的区别：wait 进程、捕获 stdout/stderr、保留退出码，
    把 PowerShell 报错原文直接交给用户。
    """
    aumid = resolve_aumid()
    values = _read_registry_values(aumid)
    print("=" * 46)
    print("HQin Harness 通知诊断")
    print("=" * 46)
    print(f"平台: {sys.platform} | Python {sys.version.split()[0]}")
    print(f"AppId(AUMID): {aumid}")
    lnk_exists = os.path.isfile(_self_lnk_path()) if sys.platform == "win32" else False
    print(f"自注册快捷方式: {'存在（使用专用 AppId）' if lnk_exists else '不存在（--register 可创建）'}")
    print()
    print("—— 通知环境（只读注册表） ——")
    print(registry_verdict(values))
    print()
    print("—— 同步发送测试 toast ——")
    if sys.platform != "win32":
        print("非 Windows 平台，toast 不可用。")
        return 1
    try:
        result = subprocess.run(
            build_ps_args(title, message, aumid, _resolve_sound()),
            shell=False,
            capture_output=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            creationflags=_creation_flags(),
        )
        out = result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else (result.stdout or "")
        err = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else (result.stderr or "")
        exit_code = result.returncode
    except PermissionError:
        # 某些沙盒禁止 CreatePipe：捕获输出失败时退化为只看退出码。
        print("无法捕获子进程输出（当前环境禁止管道），改为 DEVNULL 重试……")
        try:
            result = subprocess.run(
                build_ps_args(title, message, aumid, _resolve_sound()),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
                creationflags=_creation_flags(),
            )
            out, err, exit_code = "", "", result.returncode
        except Exception as exc:
            print(f"powershell 启动失败: {exc}")
            return 1
    except Exception as exc:
        print(f"powershell 启动失败: {exc}")
        return 1
    print(f"powershell 退出码: {exit_code}")
    if out.strip():
        print(f"stdout 原文:\n{out.strip()}")
    if err.strip():
        print(f"stderr 原文（报错关键在这）:\n{err.strip()}")
    print()
    print("—— 排查建议 ——")
    for i, tip in enumerate(advise_from_error(err, exit_code, values), 1):
        print(f"{i}. {tip}")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：python -m notifier [标题] [正文] | --register"""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--register":
        ok, detail = do_register()
        print(detail)
        return 0 if ok else 1
    title = args[0] if args else "HQin Harness"
    message = " ".join(args[1:]) if len(args) > 1 else "通知链路诊断：看到这条说明 toast 已送达"
    return diagnose_send(title, message)


if __name__ == "__main__":  # pragma: no cover - CLI 由用户手动运行
    sys.exit(main())
