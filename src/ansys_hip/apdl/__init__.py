"""APDL 资产:jinja2 求解模板(轴对称/3D)与材料库。

- template_*.inp 由 kernels/fem.py 渲染后交 runner.py 以 ansys221 -b 批处理执行
- material_lib.py 提供 20 钢/TC4 文献初值(温度相关),请求参数可逐项覆盖
"""
