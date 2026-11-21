# 地表温度、植被与土壤湿度分析 WebGIS

项目以公开遥感数据的地图浏览、查询、统计、对比和导出为开发目标，不以复现论文为交付条件。已用真实数据跑通：数据清单与校验 → NDVI/温度预处理 → SMAP原始HDF5读取 → EF基线试算 → WebGIS图层显示和点击查询。原始目录 first_data 只读；处理结果默认写到 data/sample。现有EF作为可选派生图层保留；微波年度优化和ARM站点验证移出本次必做范围。

当前范围与功能优先级以 docs/项目范围与开发路线.md 为准。弱化文献复现不取消原作业对架构、地图服务集成、缓存、安全、性能和提交材料的要求。

作业文档入口：[作业提交材料总目录](docs/作业提交材料总目录.md)。已整理[课程项目报告](docs/课程项目报告.md)、[提交说明](docs/提交说明.md)、[安全测试报告](reports/安全测试报告.md)和两份协作/审查日志模板；未完成的仓库、日志、视频、MVT专项与跨机复现均明确标记。

2026-11-21功能更新：日期播放、真实QA质量诊断、PNG/PDF地图成果和同点三变量时序均已有验收记录，操作见docs/网页使用说明.md，证据见reports/演示与成果导出验收-20261121.md。LST当前发布与显示单位为°C；原始数据及EF内部温度计算仍按各自规定保留K。

## 本地运行

需要 Python 3.12、Node.js 22 和 pnpm。本机已有虚拟环境和前端依赖，可直接使用后续启动命令。首次部署在项目根目录安装依赖：

    py -3.12 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r backend\requirements-lock.txt
    Set-Location frontend
    pnpm install --frozen-lockfile
    Set-Location ..

一键启动本机课程演示（API、GeoServer/GWC和网页）需 Java 17，并在项目根目录PowerShell执行：

    Set-ExecutionPolicy -Scope Process Bypass
    .\scripts\start-webgis.ps1

浏览器打开 http://127.0.0.1:5175；API文档为 http://127.0.0.1:8002/api/docs，GeoServer为 http://127.0.0.1:8600/geoserver/web/。脚本只绑定127.0.0.1。详细缓存、安全、性能验收说明见 docs/工程验收与启动.md。若只需运行数值查询的本地栅格回退模式，可继续使用旧的 start-api.ps1 与 start-frontend.ps1。Docker用户也可运行 .\scripts\start.ps1，首次构建需要网络；Compose将 data/sample 只读挂载给API。Docker路径尚未在本机验收。

## 处理本机真实样本

确认原始数据位于 first_data 后，在项目根目录运行：

    .\scripts\process-sample.ps1

脚本会完成处理并自动逐层校验API查询值与真实文件像元是否一致。可增加 -HashFiles 为原始文件生成SHA256，数据量大时耗时会增加：

    .\scripts\process-sample.ps1 -HashFiles

输出位置：

- data/sample/input_manifest.json：文件大小、格式头、栅格元数据和可选SHA256清单。
- data/sample/processing_report.json 与 处理结果说明.md：逐日缺测、筛选、源文件、拟合诊断、参数及限制。
- data/sample/rasters：已缩放并掩膜的GeoTIFF；data/sample/quality：EF逐像元QC编码。
- data/sample/map_rasters：真实重投影到EPSG:3857的最近邻展示文件，仅供地图服务；分析和数值下载仍使用rasters中的原网格。
- data/sample/vectors：SMAP AM原生粗网格中心点GeoJSON；缺测日保留空文件。
- data/sample/previews：适配Web Mercator地图的透明PNG预览。
- data/sample/catalog.json：实际可发布图层，网页仅加载该清单中的ready资产。
- data/sample/verification.json：真实文件和API交叉校验，以及可用于点击查询的示例坐标。

2026年11月15日更新：清点384个输入文件，当前发布观测期为2025-05-01至2025-08-31，共332个图层。旧2019年发布保留在data/releases备份目录，原始文件未改动。最新数量与缺测原因以处理报告为准：

| 变量 | 发布日期数 | 观测范围与说明 |
|---|---:|---|
| NDVI | 121 | 2025-05-01至08-29；合成期插值，08-30/31不外推 |
| LST | 106 | 2025-05-01至08-31；6天缺文件，11天无QA有效值 |
| SMAP AM | 15 | 2025-06-01至07-17；34份增强产品中15天在研究区有QA=0值 |
| EF | 90 | 可选工程基线，保留拒绝拟合及缺测原因 |

网页默认显示NDVI，可切换LST、SMAP和可选EF工程基线。现在支持覆盖率与低覆盖提示、变量分组日期筛选、点位逐日时序、矩形/多边形区域统计、双日期同步对比、透明度调整、CSV导出和真实GeoTIFF/GeoJSON下载。317个栅格日期使用GeoServer/GWC瓦片，服务不可用时安全回退本地瓦片；点击栅格读取源像元，点击SMAP查询30公里以内最近有效原生格点。地图使用本地Natural Earth水系、州界与城市背景，研究区外淡化、边界采用细实线；缺测处保留底图，初始视野按真实AOI自动缩放，无需外部底图密钥。完整操作和启动说明见 docs/网页使用说明.md 与 docs/工程验收与启动.md。

处理器按文件名递归识别MODIS，不依赖下载文件所在子目录；默认选择最新MOD11A1观测年份，也可以使用scripts/process-sample.ps1 -Year 2025。脚本先在临时发布目录处理并校验，通过后保留旧发布并切换data/sample；加-PublishMapServices可上传到已启动的本机GeoServer。SMAP支持标准SPL3SMP和增强SPL3SMP_E，并从HDF5元数据记录产品版本及网格间距；当前增强产品为SPL3SMP_E.006、约9公里间距，不代表独立的9公里空间分辨率。

## 方法和限制

- MOD11A1：按文件比例因子缩放至K；筛选QC低两位为0、数据质量位为0、温度误差位≤1，再按AOI掩膜。MOD11A2不混入逐日计算。
- MOD13A2：检查网格一致，MODLAND QA=0、pixel_reliability=0、陆地位=1、无雪/阴影。使用前后两期合成的名义起始日线性插值；不是每日NDVI观测。
- EF：植被覆盖度分箱取干边分位数，线性拟合干边并取湿边分位数；需要足够分箱，拒绝非递减干边。相对干燥度和EF越界像元标记无效，不强行截断。参数和QC编码写入报告。
- EF使用地表温度代替气温计算斜率项，是工程基线，不能声称复现论文或完成精度验证。SMAP本轮独立发布，没有参与EF微波校准或下尺度。
- SMAP：读取HDF5中的AM土壤湿度、网格中心经纬度和retrieval_qual_flag，仅保留flag=0及物理范围内的值。历史异常1×1 TIFF若重新放入，会被隔离，不参与处理。
- MOD03/MOD06/MOD07仅清点和核对日期/过境时刻；集合号差异记录在报告中，本轮EF不依赖这些轨道文件。无ARM数据，不生成站点验证指标。
- 区域统计按研究区内源像元中心计数，保留文件掩膜和缺测；仅有效值参与统计。SMAP区域统计不虚构缺失格点位置，因此区域有效比例留空。各变量日期对比使用统一色标。
- GeoServer/GWC、本地有界缓存、输入边界和缓存性能对照已有验收记录；TLS、公网用户认证、30秒正式负载及跨机部署仍未完成。论文复现、年度优化及站点精度验证属于可选研究扩展。

## 验证

    .\scripts\verify.ps1
    .\.venv\Scripts\python.exe -m scripts.verify_sample_release

前一命令运行后端测试、前端类型检查和生产构建；后一命令读取真实发布文件，校验所有图层查询及栅格的覆盖率和区域统计。单元测试中的人工数组仅验证程序行为。scripts/verify-browser.cjs是2019年旧样本的浏览器验收脚本，其中日期和数量尚为旧样本断言，不适用于当前2025年发布。当前本机Vite实例使用scripts/verify-current-release.cjs，按实际目录选择日期并等待MapLibre完成绘制；scripts/verify_map_projection.py用于逐层对照GeoWebCache与本地展示投影。当前批次记录见reports/数据更新-20261115.md。

开发优先级见 docs/项目范围与开发路线.md，实际状态见 docs/requirements.md；原全栈文档中的研究章节仅作为可选技术参考。
