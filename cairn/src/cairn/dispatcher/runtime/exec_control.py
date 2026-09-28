"""容器内 exec 的 supervisor 与终止助手，均在原镜像 Python 上执行。"""

# 同一 /proc 判定同时用于 supervisor、信号助手和退出核验，zombie 已不能继续执行。
_PROC_STATE = r'''
def state(pid):
    with open('/proc/%s/stat' % pid) as f:
        return f.read().rsplit(')', 1)[1].split()
def live_group(group):
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            fields = state(name)
        except FileNotFoundError:
            continue
        if int(fields[2]) == group and fields[0] not in ('Z', 'X'):
            return True
    return False
'''

# Docker inspect.Pid 属于宿主机命名空间，绝不能直接交给容器内 kill。
# supervisor 是 subreaper：不同 SID 的工具孤儿仍归属本次 exec，不能仅凭 runner 组为空证明停止。
EXEC_SUPERVISOR = 'import ctypes, json, os, signal, sys, time\n' + _PROC_STATE + r'''
path, command = sys.argv[1], sys.argv[2:]
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
    raise OSError(ctypes.get_errno(), 'cannot establish exec subreaper')

def children():
    # Linux 的 task/children 是内核直接子进程集合；无法读取时绝不写排空凭据。
    with open('/proc/%s/task/%s/children' % (os.getpid(), os.getpid())) as f:
        return [int(pid) for pid in f.read().split()]

def adopted():
    result = []
    for pid in children():
        if pid == child:
            continue
        try:
            fields = state(pid)
        except FileNotFoundError:
            continue
        if int(fields[1]) != os.getpid():
            raise RuntimeError('exec child identity changed')
        if fields[0] in ('Z', 'X'):
            os.waitpid(pid, os.WNOHANG)
        else:
            result.append((pid, fields[19]))
    return result

def write_identity(identity):
    temporary = path + '.writing'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(identity, f)
        f.flush()
        os.fsync(f.fileno())
    os.rename(temporary, path)
    directory = os.open(os.path.dirname(path) or '.', os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)

child = os.fork()
if child == 0:
    os.setsid()
    write_identity({'version': 2, 'pid': os.getpid(), 'start': state(os.getpid())[19],
                    'parent': os.getppid(), 'parent_start': state(os.getppid())[19]})
    if os.path.exists(path + '.cancel'):
        os._exit(130)
    os.execvpe(command[0], command, os.environ)

# 不回收 runner 的 zombie，固定 PGID 和身份直到所有后代都排空。
while True:
    info = os.waitid(os.P_PID, child, os.WEXITED | os.WNOWAIT | os.WNOHANG)
    if info is not None:
        break
    adopted()
    time.sleep(.02)
code = info.si_status if info.si_code == os.CLD_EXITED else 128 + info.si_status
try:
    os.killpg(child, signal.SIGTERM)
except ProcessLookupError:
    pass
term_deadline = time.monotonic() + 1.5
while True:
    descendants = adopted()
    if not descendants and not live_group(child):
        # 再观察一次直接子进程集合；它为空意味着不存在可在稍后重新归属的任务后代。
        if not adopted():
            break
    sig = signal.SIGTERM if time.monotonic() < term_deadline else signal.SIGKILL
    try:
        os.killpg(child, sig)
    except ProcessLookupError:
        pass
    for pid, start in descendants:
        try:
            fields = state(pid)
            if int(fields[1]) == os.getpid() and fields[19] == start:
                os.kill(pid, sig)
        except FileNotFoundError:
            continue
    time.sleep(.02)
with open(path) as f:
    identity = json.load(f)
write_identity({**identity, 'drained': True, 'returncode': code})
os.waitpid(child, 0)
os._exit(code)
'''

# 先落取消标记，再等待 child 身份；覆盖 exec_start 已返回但 pidfile 尚未写出的窗口。
# 只使用精确容器内 namespace PID，并核验 parent/starttime/session/group。
EXEC_TERMINATOR = 'import json, os, signal, sys, time\n' + _PROC_STATE + r'''
path = sys.argv[1]
fd = os.open(path + '.cancel', os.O_WRONLY | os.O_CREAT, 0o600)
os.close(fd)
deadline = time.monotonic() + 5
while True:
    try:
        with open(path) as f:
            identity = json.load(f)
        break
    except FileNotFoundError:
        if time.monotonic() >= deadline:
            sys.exit(2)
        time.sleep(.02)
pid, parent = identity['pid'], identity['parent']
def owned():
    if identity.get('version') != 2 or identity.get('drained') is True:
        return False
    try:
        fields, ancestor = state(pid), state(parent)
    except FileNotFoundError:
        return False
    return (fields[19] == identity['start'] and ancestor[19] == identity['parent_start']
            and ancestor[0] not in ('Z', 'X')
            and int(fields[1]) == parent and int(fields[2]) == pid and int(fields[3]) == pid)
# 给 pi 的 abort/finally/lease.finish 有限落盘机会，超时仍按原精确进程组硬停。
for sig, grace in ((signal.SIGTERM, 8), (signal.SIGKILL, 2)):
    if not owned():
        sys.exit(2)
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        sys.exit(2)
    deadline = time.monotonic() + grace
    while live_group(pid):
        if time.monotonic() >= deadline:
            break
        time.sleep(.02)
    else:
        sys.exit(0)
sys.exit(2)
'''

# supervisor 若被外部信号/OOM 直接杀死，Docker terminal 本身不能证明后代已停。
# 两种检查共用严格的 v2 control file，旧 PGID-only 凭据不能升级为后代证明。
_EXEC_CONTROL_IDENTITY = 'import json, os, stat, sys\n' + _PROC_STATE + r'''
path = sys.argv[1]
try:
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
except FileNotFoundError:
    sys.exit(2)
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
        sys.exit(2)
    with os.fdopen(fd) as f:
        fd = -1
        identity = json.load(f)
finally:
    if fd >= 0:
        os.close(fd)
if (not isinstance(identity, dict) or type(identity.get('version')) is not int
        or identity['version'] != 2 or type(identity.get('pid')) is not int or identity['pid'] <= 0
        or type(identity.get('parent')) is not int or identity['parent'] <= 0
        or not isinstance(identity.get('start'), str) or not identity['start'].isdigit()
        or not isinstance(identity.get('parent_start'), str) or not identity['parent_start'].isdigit()):
    sys.exit(2)
'''

EXEC_VERIFY_RUNNING = _EXEC_CONTROL_IDENTITY + r'''
if set(identity) != {'version', 'pid', 'start', 'parent', 'parent_start'}:
    sys.exit(2)
try:
    runner, ancestor = state(identity['pid']), state(identity['parent'])
except FileNotFoundError:
    sys.exit(2)
if (runner[0] in ('Z', 'X') or ancestor[0] in ('Z', 'X')
        or runner[19] != identity['start'] or ancestor[19] != identity['parent_start']
        or int(runner[1]) != identity['parent']
        or int(runner[2]) != identity['pid'] or int(runner[3]) != identity['pid']):
    sys.exit(2)
'''

_EXEC_VERIFY_DRAINED = _EXEC_CONTROL_IDENTITY + r'''
required = {'version', 'pid', 'start', 'parent', 'parent_start', 'drained', 'returncode'}
if (set(identity) != required or identity.get('drained') is not True
        or type(identity.get('returncode')) is not int
        or live_group(identity['pid'])):
    sys.exit(2)
'''

# Docker 仍保留 exec 终态时，还必须要求 supervisor returncode 与其一致。
EXEC_VERIFY_EXIT = _EXEC_VERIFY_DRAINED + r'''
if identity['returncode'] != int(sys.argv[2]):
    sys.exit(2)
'''

# exec inspect 记录已被 Docker 回收时，只使用 v2 control path 与实时空进程组确认停止。
EXEC_VERIFY_DRAINED = _EXEC_VERIFY_DRAINED

# terminal proof 的删除与验证分离；仅在 exact fence 释放后调用。
EXEC_CLEANUP = r'''
import os, sys
for path in (sys.argv[1], sys.argv[1] + '.cancel'):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
'''
