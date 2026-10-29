# pl-jira-task-import

驱动PL根据任务模板填好项目、负责人和要做的任务，它按 Epic → 适配任务 → 子任务 的顺序建立jira，把编号填回飞书，并列出结果和问题。

## 准备

- 安装 Python 3、`active-jira`、`lark-cli`，完成 Jira 和飞书登录。
- 本技能放在 `~/.codex/skills/pl-jira-task-import/`；`active-jira` 放在 `~/.codex/skills/active-jira/`。执行脚本会从后者读取认证辅助模块。
- Jira 凭据可保存在 `~/.netrc`，权限设为 `600`。
## 从这里开始

先复制[模板](https://zepp.feishu.cn/wiki/C9mvwKf4ai01xMkOlSrcuCSCngc?sheet=2fJqWn)，核对负责人的 Jira 账号和阶段版本，并修改 Epic 标题中的项目名称。增加模块或阶段后，要同步检查筛选和上级公式。

| 工作表 | 主要做什么 |
| --- | --- |
| 模板使用入口 | 记录项目和本次导入的名称；其他表的内容也要同步修改 |
| 项目人员信息 | 填人员账号、PL 角色，以及阶段对应的版本 |
| Epic | 核对名称、负责人和阶段；编号由助手写入 G 列 |
| 适配任务 | 选择适用的模块和阶段，核对负责人；编号由助手写入 J 列 |
| 适配子任务 | 核对标题、描述、负责人、阶段和上级公式 |
| 导入到Jira | 查看说明和字段配置附件 |
| 新功能开发任务 | 有新需求时另行指定范围；当前三层执行脚本不自动处理 |

## 怎么提问


只检查时，可以这样说：

> 使用 $pl-jira-task-import，检查〈表格链接〉中当前筛选的任务。项目是 FUJI。先列出数量、负责人和问题，给我预览。

准备创建时，可以这样说：

> 使用 $pl-jira-task-import，项目是 FUJI，表格是〈链接〉。从空编号开始，按当前筛选依次创建 Epic、适配任务和子任务，写回编号并核对结果。


## 遇到问题怎么办

- 表格筛选排除的任务会跳过。
- 上级编号为空或报错时，先完成上级任务，再检查该行。
- 负责人账号或版本填错时，会指出问题，不随意补值。
- 中途失败时，沿用原来的批次名和日志目录，先核对已完成任务，再继续。

“批次”就是这次导入的名称，用来记录进度和避免重复建单。新一轮创建使用新名称，中断后继续使用原名称。

## 文件在哪里

```text
pl-jira-task-import/
├── README.md                    快速上手
├── SKILL.md                     助手执行规则
├── agents/openai.yaml           界面介绍
├── references/
│   └── template-contract.md     表格规则和字段对应关系
└── scripts/
    ├── snapshot_template.py     读取表格和筛选
    ├── plan_import.py           生成预览和CSV
    └── import_stage.py          创建一层任务并写回编号
```

2026-10-28 已用 Fuji 项目完成三层建单和编号回填，16 项行为测试通过。普通 Jira 查询使用 `active-jira`。
