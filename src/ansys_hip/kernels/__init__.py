"""方法内核包 — 每个模块提供若干 run_<method>(params, ctx) -> dict。

子模块(与 registry.KERNEL_MODULES 对应):
    arrhenius   densification / process-window / sensitivity
    shrinkage   shrinkage-estimate / compensate
    calibrate   calibrate
    materials   material-query(数据在 data/materials.yaml)
    fem         axisym-hip / axisym-thermal / axisym-mechanical / full3d-hip / mesh

约定(见 schemas.RunContext 与 registry.KernelError):
    - 同步函数,由队列在工作线程调用;
    - 返回 dict 必含 "fidelity" 键,工件写入 ctx.job_dir 并在 "artifacts" 列出;
    - 主动失败抛 KernelError(code, message)。
"""
