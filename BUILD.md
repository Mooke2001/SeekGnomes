# 构建与发布

SeekGnomes v1.3.0使用Linux amd64容器. 准备Docker Engine或Linux rootless Podman, Python 3.11或更新版本及可访问固定依赖来源的网络. Windows可在WSL2中走同一流程. 预留至少40 GiB容器及构建空间.

GitHub源码含构建配方及依赖锁. 完整构建包另含Lean、Mathlib和固定Python wheel. 原生harness运行时由使用者在构建机上按固定版本从相应上游获取, 并遵循各厂商条款. 公开归档只提供原生安装配方、版本锁和核验元数据.

下载[完整构建包](https://github.com/Mooke2001/SeekGnomes/releases/download/v1.3.0/seekgnomes-v1.3.0-public-build-context.tar.gz), 然后解压并进入构建目录. 完整归档的SHA-256见[校验清单](https://github.com/Mooke2001/SeekGnomes/releases/download/v1.3.0/seekgnomes-v1.3.0-SHA256SUMS).

```bash
tar -xzf seekgnomes-v1.3.0-public-build-context.tar.gz
cd seekgnomes-v1.3.0
```

在构建目录中执行以下命令.

```bash
python3 build-harness-assets.py
python3 build-zcode-assets.py
python3 seal-native-assets.py
./build-image.sh --engine docker
./seekgnomes doctor
./seekgnomes self-test
```

使用Podman构建时, 执行`./build-image.sh --engine podman`. 同一终端运行`export SEEKGNOMES_ENGINE=podman`, 让后续启动器使用同一个容器引擎. `Dockerfile`从固定的Debian/Python镜像开始, 从本地锁定的wheelhouse安装Python依赖. Harbor装在`/opt/seekgnomes/harbor-deps`并与科学Python环境隔离. 原生资产必须通过SHA-256清单核验. 镜像运行及自检期间不需要下载原生harness, `doctor`和`self-test`不调用模型API.

完成自检后用`./seekgnomes`在终端配置并启动测评, 或运行`bash ./console-src/Start.command`打开本地浏览器控制台.

镜像相关验证摘要绑定被测源码清单与实际镜像ID. 复建镜像有自己的镜像ID, 因此请对复建结果执行上述自检. 本项目不承诺独立构建生成相同字节.

## 作者侧封装

在项目目录使用`python3 revisions/seekgnomes_v1_2/build_bundle.py`生成独立的`releases/seekgnomes-v1.3.0`上下文. `--without-assets`只准备轻量文件. 大型资产来自封存的v1.2.0依赖目录. 源码采用本地工作区的v1.3.0协议. 先完成自检并生成绑定镜像和源码的`validation-summary.json`, 再用`--archives`组合归档和分卷. `source-manifest.json`给出锁定文件的SHA-256及控制台源码清单.
