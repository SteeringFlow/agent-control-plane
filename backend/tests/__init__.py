"""后端测试包。

Step 5.1 的三类验收：
- test_health.py            运行链 A/B/C（可启动、/health、OpenAPI 暴露）
- test_import_boundaries.py 验收 D（导入边界无反向依赖）
- test_settings.py          配置最小集与环境变量覆盖
"""
