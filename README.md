# TCX to FIT

将华为运动健康导出的 TCX 文件转换为可导入行者的标准 Garmin FIT 活动文件。

## 安装

```powershell
python -m pip install -r requirements.txt
```

需要 Python 3.8 或更新版本。

## 转换单个文件

```powershell
python tcx_to_fit.py "C:\path\to\activity.tcx"
```

默认在 TCX 文件旁生成同名 `.fit` 文件。转换后会自动通过 FIT 文件头、CRC 和消息解码校验。

## 批量转换

```powershell
python tcx_to_fit.py "C:\Huawei\TCX" --output "C:\Huawei\FIT"
```

## 数据处理

- 保留轨迹点时间、GPS、海拔；如源文件包含，也保留心率和踏频。
- 优先使用 TCX 的逐点距离/速度；缺少时按 GPS 轨迹计算。
- 华为文件只有 Lap 总距离时，仍保留华为报告的总距离。
- 生成 FIT 活动所需的 `file_id`、`record`、`lap`、`session` 和 `activity` 消息。

## 依赖

使用 Garmin 官方开源 [FIT Python SDK](https://github.com/garmin/fit-python-sdk)。
