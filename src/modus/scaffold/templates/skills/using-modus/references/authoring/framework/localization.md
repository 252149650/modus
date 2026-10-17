# 正文语言与本地化

正文默认使用简体中文，覆盖标题、解释、导航、表格与图表标签。只有用户明确指定其他语言时才切换；不根据源码注释、输入材料或会话语言自动切换。

一个知识树保持主要语言一致。reinit 遇到历史外语正文时，在本次已确认范围内统一为中文；局部更新只修订授权范围，保留无关文档与人工维护内容。混合材料先归一语义，再用目标语言表达。

文件路径、Domain/Module slug、源码标识、协议名、配置键、枚举传输值和命令保持原样。自然标题、解释和读者导航使用目标语言，不逐字硬译，也不为了匹配预设改写仍然清晰的已有标题。

| 产物 | 简体中文起点 | 用户明确要求英文时 |
| --- | --- | --- |
| 根概览 | 项目总览；系统架构；能力地图；知识导航；开发与排障 | Project overview; System architecture; Capability map; Knowledge navigation; Development and troubleshooting |
| Domain | 领域定位；结果与边界；专题导航；模块全景 | Domain positioning; Outcomes and boundaries; Topic navigation; Module landscape |
| Module | 模块定位与边界；核心场景与行为；实现、扩展与验证 | Module scope; Core scenarios and behavior; Implementation, extension and verification |
| 专题 | 总览；按真实事实命名的详情 | Overview; Details named after real facts |

这些是表达起点，不是验证器要求。导航层标题可以稳定；场景 H3 应由真实任务自然命名。只有路径、身份和机器字段属于硬协议。
