# pgBackRest 灾备边车镜像
#
# 为什么自建而不是拉现成镜像：
#   1. 官方**没有**发布 `pgbackrest/pgbackrest` 镜像 —— 实测
#      2026-10-11 查询 Docker Hub，`pgbackrest/*` 下只有 `test` 与 `doc` 两个仓库，
#      `pgbackrest/pgbackrest` 返回 {"message":"object not found"}。
#      （`external/SOURCES.md:325` 先前记的镜像名是**错的**，已在本次一并纠正。）
#   2. 第三方镜像（woblerr/pgbackrest 等）内部构建不可审计，不符合 AGENTS.md「不裸信镜像名」。
#   3. 官方唯一推荐的安装路径就是 **PGDG 包仓库**（apt.postgresql.org）：
#      external/pgbackrest/doc/xml/user-guide.xml:586
#        "Ubuntu packages for pgBackRest are available at apt.postgresql.org"
#      安装方式实证（同文件 :343-344）：先装 postgresql-common，再跑 PGDG 仓库脚本。
#
# 基础镜像：官方 Debian slim（非 postgres 镜像）。
# 为什么不用 postgres:16 —— 该镜像的 ENTRYPOINT 是 docker-entrypoint.sh，
# 会试图初始化一个 PG 集群，与「纯备份客户端」的用途冲突（我们不需要本机 PG）。
# pgbackrest 只需 libpq 等运行时库，故用 debian:bookworm-slim + PGDG 包。
# PG 大版本一致性由「连的是 PG16 + pgbackrest 客户端协议」保证，不需要本机装 PG。
FROM debian:bookworm-slim

# 官方安装路径：PGDG 仓库（user-guide.xml:343-344 的 pgdg 脚本）
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-common ca-certificates gnupg curl lsb-release \
 && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y \
 && apt-get install -y --no-install-recommends pgbackrest \
 # 镜像内验证二进制存在，否则构建即失败（不让「配置了但没装」的假成功溜过去）
 && pgbackrest version \
 && rm -rf /var/lib/apt/lists/*

# 官方要求的目录布局（user-guide.xml:626-637）：日志目录 / 配置目录
# 注：mkdir -p -m 对中间目录不生效（hadolint SC2174），故分两步先建再改权限
# postgres 用户：postgresql-common 通常会建；此处显式兜底保证 USER 指令可解析
RUN id postgres >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash postgres \
 && mkdir -p /var/log/pgbackrest /etc/pgbackrest /var/lib/pgbackrest \
 && chmod 770 /var/log/pgbackrest

# 配置**不 COPY 进镜像** —— 由 compose 只读挂载（见 docker-compose.dr.yml）。
# 好处：改配置不必重建镜像；且镜像里不含任何口令注入点，降低误烘焙密钥的风险。
RUN chown postgres:postgres /etc/pgbackrest /var/log/pgbackrest /var/lib/pgbackrest

# 以 postgres 身份运行（pgbackrest 不允许以 root 跑非 restore 命令，
# 实证：external/pgbackrest/doc/xml/reference.xml:164）
USER postgres

# 默认空转；实际命令由 compose 的 command 覆盖（见 docker-compose.dr.yml）
CMD ["sleep", "infinity"]
