<!--
本页只适配取证方法，领域身份、稳定路径与正文职责由共享框架维护。
-->

# Kotlin 证据适配

以 Gradle target、source set、JVM/Native/JS 平台和发布单元识别边界；从 Spring/Ktor 入口、命令、public API、任务和 Flow 消费端调查。核对 data/sealed class、序列化、`suspend`/Flow、coroutine scope、dispatcher、取消、跨协程事务、资源释放和 Java interop。

不得把 suspend 调用自动描述为并发或后台执行；必须确认调用上下文、scope 所有者和完成/取消传播。共享源码集也不自动证明各 target 行为一致。
