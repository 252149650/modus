<!--
本页只适配取证方法，领域身份、稳定路径与正文职责由共享框架维护。
-->

# C / C++ 证据适配

以 CMake/Make/Bazel target、公开 header、链接边界和 build variant 识别单元；从 executable、export、命令、callback、event loop 与 plugin ABI 调查。核对结构体/序列化、指针与 ownership、RAII/析构、错误码/异常、线程/锁、句柄释放、编译宏、配置和测试。

宏、函数指针、条件编译和动态链接关系必须标明证据强度；header 声明不自动证明符号在当前产物中导出或 ABI 兼容。
