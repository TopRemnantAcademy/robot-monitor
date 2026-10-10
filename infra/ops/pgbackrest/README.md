# pgBackRest 接入说明（未验证 · 待订单）

## 现状（事实）

`infra/docker-compose.yml` 的 `timescaledb` 用的是 `timescale/timescaledb:2.13.1-pg16`。
该镜像**不含 pgbackrest**。因此本目录只交付配置契约，**没有**接线进 compose。

## 为什么值得做

`infra/init.sql` 里的 `robot_telemetry` 是复合主键 `(time, robot_id)` 的超表，
100 台 × 10Hz ≈ **8.64M 行/天**。现在整个仓库**没有任何备份路径**：
`docker compose down -v` 一次，历史数据归零，且看板的「历史折线」永不可复现。

这属于监控系统里典型「出事前没人提」的缺口。

## 接入步骤（照做即可，需验证）

1. 换镜像或加边车：把 `timescale/timescaledb:2.13.1-pg16` 换成自带 pgbackrest 的镜像，
   或起一个 `pgbackrest/pgbackrest` 容器（**镜像名未验证**，需先 `docker manifest inspect`，
   参照本仓库 `external/SOURCES.md` 里 EMQX 镜像名的 digest 教训 —— 别裸信名字）。
2. 挂配置：`./ops/pgbackrest/pgbackrest.conf → /etc/pgbackrest/pgbackrest.conf`。
3. 初始化仓库：`pgbackrest --stanza=robot_monitor stanza-create`。
4. 验证：`pgbackrest --stanza=robot_monitor check`，再 `--type=full backup`。
5. 把 `repo1-cipher-pass` 落到 `.env`（**绝不入库**）。

## 未验证清单

- pgbackrest 官方镜像名与 tag（未 `docker manifest inspect`）
- TimescaleDB 超表在 pgbackrest 物理备份下的恢复一致性（社区有讨论，本项目未实测）
- 8.64M 行/天 的实际备份窗口与 `process-max=2` 是否够
