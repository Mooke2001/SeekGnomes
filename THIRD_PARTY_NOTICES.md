# 第三方组件

公开构建上下文保留Lean发行版的`LICENSE`和`LICENSES`、Mathlib及其依赖仓库的许可文件和Python wheel的许可元数据. 科学计算环境的版本见`requirements-science.lock`, Harbor 0.22.0及其依赖见`requirements-harbor.lock`. [Harbor采用Apache-2.0许可证](https://github.com/harbor-framework/harbor).

厂商CLI及其依赖由使用者在本机按固定版本从上游获取. 来源和安装元数据见`harness_registry.json`和`HARNESS_SOURCES.md`. 这些组件各自适用其上游条款, 包括[Claude Code的条款](https://code.claude.com/docs/en/legal-and-compliance). 构建后的本地镜像会保留上游资产随附的许可文件.

SeekGnomes自有代码和附带文档采用[MIT许可证](LICENSE).
