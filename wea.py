import requests
import pandas as pd

# =========================
# 1. 配置区域
# =========================

# 填写电站经纬度
LATITUDE = 23
LONGITUDE = 113

# 查询时间范围
START_DATE = "2026-05-01"
END_DATE = "2026-08-31"

# 输出文件
OUTPUT_FILE = "历史天气数据.csv"


# =========================
# 2. Open-Meteo API
# =========================

url = "https://archive-api.open-meteo.com/v1/archive"

params = {
    "latitude": LATITUDE,
    "longitude": LONGITUDE,
    "start_date": START_DATE,
    "end_date": END_DATE,

    "hourly": ",".join([
        "temperature_2m",
        "relative_humidity_2m",
        "cloud_cover",
        "cloud_cover_low",
        "cloud_cover_mid",
        "cloud_cover_high",
        "shortwave_radiation",
        "direct_radiation",
        "diffuse_radiation",
        "precipitation",
        "wind_speed_10m",
    ]),

    "timezone": "Asia/Shanghai",
}


# =========================
# 3. 请求数据
# =========================

print("正在下载历史天气数据...")

response = requests.get(
    url,
    params=params,
    timeout=60,
)

response.raise_for_status()

data = response.json()

if "hourly" not in data:
    raise RuntimeError(f"API 返回异常: {data}")


# =========================
# 4. 转成 DataFrame
# =========================

df = pd.DataFrame(data["hourly"])

df["time"] = pd.to_datetime(df["time"])

# 改成中文列名
df = df.rename(
    columns={
        "time": "时间",
        "temperature_2m": "气温(℃)",
        "relative_humidity_2m": "相对湿度(%)",
        "cloud_cover": "总云量(%)",
        "cloud_cover_low": "低层云量(%)",
        "cloud_cover_mid": "中层云量(%)",
        "cloud_cover_high": "高层云量(%)",
        "shortwave_radiation": "短波辐射(W/㎡)",
        "direct_radiation": "直接辐射(W/㎡)",
        "diffuse_radiation": "散射辐射(W/㎡)",
        "precipitation": "降水量(mm)",
        "wind_speed_10m": "10米风速(km/h)",
    }
)


# =========================
# 5. 保存 CSV
# =========================

df.to_csv(
    OUTPUT_FILE,
    index=False,
    encoding="utf-8-sig",  # Excel 打开中文不会乱码
)

print()
print("下载完成")
print(f"经纬度: {LATITUDE}, {LONGITUDE}")
print(f"时间范围: {START_DATE} ~ {END_DATE}")
print(f"数据条数: {len(df)}")
print(f"输出文件: {OUTPUT_FILE}")

print()
print("前 10 条数据：")
print(df.head(10).to_string(index=False))
