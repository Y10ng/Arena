"""Docker stats 原始负载解析工具。"""

from __future__ import annotations


def cpu_percent_from_stats(stats: dict[str, object]) -> float | None:
    # stats 结构随 Docker 版本和容器状态变化，缺字段时返回 None 而不是影响监控聚合。
    cpu_stats = stats.get("cpu_stats")
    precpu_stats = stats.get("precpu_stats")
    if not isinstance(cpu_stats, dict) or not isinstance(precpu_stats, dict):
        return None
    cpu_usage = cpu_stats.get("cpu_usage")
    precpu_usage = precpu_stats.get("cpu_usage")
    if not isinstance(cpu_usage, dict) or not isinstance(precpu_usage, dict):
        return None
    total_usage = number_value(cpu_usage.get("total_usage"))
    previous_total = number_value(precpu_usage.get("total_usage"))
    system_usage = number_value(cpu_stats.get("system_cpu_usage"))
    previous_system = number_value(precpu_stats.get("system_cpu_usage"))
    if None in (total_usage, previous_total, system_usage, previous_system):
        return None
    cpu_delta = total_usage - previous_total
    system_delta = system_usage - previous_system
    online_cpus = number_value(cpu_stats.get("online_cpus"))
    if online_cpus is None:
        percpu_usage = cpu_usage.get("percpu_usage")
        online_cpus = len(percpu_usage) if isinstance(percpu_usage, list) and percpu_usage else 1
    if cpu_delta <= 0 or system_delta <= 0 or online_cpus <= 0:
        return None
    return round((cpu_delta / system_delta) * online_cpus * 100, 2)


def number_value(value: object) -> float | None:
    # bool 是 int 的子类，但 Docker 统计里布尔值代表坏数据，不能参与数值计算。
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def int_value(value: object) -> int | None:
    number = number_value(value)
    if number is None:
        return None
    return int(number)
