"""HTTP 入口层（FastAPI）。

划分：

- ``management/`` 控制面：Agent / Channel / Connection / Routing / Policy
  等资源的管理与配置接口。
- ``runtime/``   数据面：渠道消息入站、ACP Agent 出站等运行时流量接口。
- ``health.py``  进程级存活探针。

为什么 health 不放在 management/ 或 runtime/ 下？
因为存活探针不属于任何纵向切片：它是编排层（docker-compose / K8s）探测
「进程是否活着」的平台级入口，既不代表控制面可用，也不代表数据面可用。
把它塞进任一子包都会让「runtime 挂了但进程活着」这类语义变得含糊。
该决定是可逆的——若后续需要 /runtime/ready 之类的细粒度探针，
再在 runtime/ 下增加即可，health.py 仍保留为进程级入口。
"""
