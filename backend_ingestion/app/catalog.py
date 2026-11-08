"""支持的机器人机型目录（DB 驱动 + 可扩展静态元数据）。

设计（用户决策 2026-11-09：「DB 基础 HUMANOID-24DOF + 扩展型 카탈로그」）：
- 「平台支持哪些机型」的**事实来源**是 `robot_metadata.model_type` 的去重值（DB 驱动，零幻觉）。
- 本模块的静态 dict 只补充**展示用元数据**（display_name / 描述 / 自由度 / 形态 / 厂商），
  键为 model_type。新增机型只需在此追加一行，查询逻辑无需改动。
- 若 DB 里出现本 dict 未覆盖的 model_type，`lookup()` 回退到「通用占位」(generic=True)，
  **绝不编造**展示信息。

元数据严格对应代码事实（避免幻觉）：
- HUMANOID-24DOF：frontend_dashboard/src/types/robot.ts 的 JOINT_NAMES 恰 24 个标准关节
  ⇒ 24 DOF；形态=双足人形（humanoid，robot_id 前缀 `humanoid-`）；厂商=本平台自研
  （无第三方来源，不编造外部厂商名）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RobotModelMeta:
    model_type: str
    display_name: str
    form_factor: str
    dof: int
    vendor: str
    description: str
    generic: bool = False


# 仅收录 DB 实测存在的机型（init.sql:48 全部 100 台预置为 HUMANOID-24DOF）。
ROBOT_CATALOG: dict[str, RobotModelMeta] = {
    "HUMANOID-24DOF": RobotModelMeta(
        model_type="HUMANOID-24DOF",
        display_name="Humanoid 24-DOF",
        form_factor="双足人形",
        dof=24,
        vendor="RobotMonitor Platform",
        description=(
            "平台主力机型：24 自由度高拟人双足机器人，覆盖肩/肘/腕/髋/膝/踝全关节遥测，"
            "支持温度、力矩、姿态角的实时监测与告警。"
        ),
    ),
}


def lookup(model_type: str | None) -> RobotModelMeta:
    """按 model_type 取展示元数据；未登记则回退占位（generic=True），不编造。"""
    if model_type and model_type in ROBOT_CATALOG:
        return ROBOT_CATALOG[model_type]
    return RobotModelMeta(
        model_type=model_type or "UNKNOWN",
        display_name=model_type or "未知机型",
        form_factor="—",
        dof=0,
        vendor="—",
        description="数据库中存在的机型，但暂未在目录元数据中登记，请联系管理员补充。",
        generic=True,
    )
