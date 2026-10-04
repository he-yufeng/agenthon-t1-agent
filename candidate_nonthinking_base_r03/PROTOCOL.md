# r03 原最好基线的单因素非thinking试验

11:56官方958959/r01 Finished0.0698，低于957271/r00的0.0930，差-0.0232。
停止推广r01复核策略，不取消/重传旧提交；原因尚未证明，不从总分推算题目数。
旧r02原型含r01策略，不直接提交它。r03改为原r00求解器、提示、默认两次尝试、
预算、模型/请求上限/代理/重试/输出政策完全不变，只通过既有241a请求控制模块
加官方enable_thinking=false布尔字段。不是新的模型/权重/adapter或low_effort设置。

核心engine1c85bae...保持字节相同，原CLI只替换客户端import指向该小型子类。
已有9项transport宿主测试不重跑；本轮新做一次原基线CLI真实loopback烟测，
固定自有GOOD程序→完整输出，确认只有一次请求且无r01复核阶段。45秒上限、
临时文件自动清理、假token，非House质量证明。

正式晋级前在独立分支/免费公开标准Linux CI构建原0f74镜像上的小overlay，
native以实际镜内engine/CLI/control字节执行9项transport+新CLI，共10项，60秒上限，
nonroot/readonly/2CPU/2GiB/64MiB noexec临时区/无外网（自有loopback可用）。
不重跑已结束87/22旧套件，不下载完整镜像到2.1GiB本机，也不安装新包/启动数据批次。
匿名核对manifest/config/新小层及继承基镜像证明后，真实Team521新HMAC/独立audit。
实际账号、Development、已接受同版本条款、可用日额度、包SHA门通过才一次上传。
本轮11:56额度0/1总2/23只是时点观测，上传前仍需重新回读；不保证未来可用。

公开源码仅自有/已许可MIT代码、own fixture和构建脚本，不含题数据/私有日志/
答案/密钥/邮箱内容。源1c85/原maina559/旧包d466/image0f74与新r01包eba22均保留。
如果r03真实官方反馈仍无改善，停止该请求控制方向，按真实差距调整优先级；
不以fake fixture或nativePASS冒称成绩，不重复等待已结束r01，不额外买资源。
