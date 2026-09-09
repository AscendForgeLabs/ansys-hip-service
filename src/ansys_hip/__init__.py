"""ansys-hip-service — HIP 仿真计算方法提供方.

面向 HIPForm 的方法级计算服务:每个 /sim/{method} 端点是一项独立计算能力,
不承载完整业务链路(无 configs/inputs 资产管理)。单位约定:mm / MPa / s / ℃。
"""

__version__ = "0.1.0"
