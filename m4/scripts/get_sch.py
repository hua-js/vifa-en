import requests

r = requests.get(
    "https://yun.vifa.cn/api/production/schedule",
    headers={"X-Api-Key": "vf_ps_9oidIoATCJkbrFywSGHihI5bYOuUG2t_EFlEylKIBCf3gDYgfivb7S48ThYZA7W9"},
    params={"startDate": "2026-09-19", "endDate": "2026-09-27"},
    timeout=30,
)
data = r.json()["data"]

# 先看排班排到哪天，超过这天没有数据是正常的
print(data["scheduleHorizonEnd"])

for row in data["rows"]:
    print(row["date"], row["deptName"], row["status"], row["plannedHours"])