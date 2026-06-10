# niannian-data 数据库规范

> niannian-data/ 是念念的"脑细胞生长土壤"。
> 所有SQLite数据库在此目录下。
> v0.2：数据库和表已创建，写脑逻辑等待LLM激活。

## 数据库清单

| 文件 | 状态 | 表 | 说明 |
|------|------|----|------|
| `knowledge.db` | ✅ 就绪 | entities, relations | 知识图谱——实体+关联 |
| `lesson.db` | ✅ 就绪 | lessons | 学到的教训——从LLM交互自动提取 |
| `facts.db` | ✅ 就绪 | facts | 事实三元组——subject/predicate/object |
| `tree.db` | ✅ 就绪 | nodes | 记忆树——层级化知识组织 |
| `forgotten.db` | ✅ 就绪 | entries | 遗忘归档——半衰期淘汰的内容 |
| `sessions.db` | ✅ 就绪 | sessions | 归档会话——历史对话存储 |
| `state.db` | ✅ 就绪 | states | 持久状态——key/value模式存储 |
| `cron.db` | ✅ 就绪 | jobs | 定时任务——schedule+prompt |
| `tasklog.db` | ✅ 就绪 | tasks | 任务日志——进化证明链 |

## 设计原则

1. **SQLite优先**：无需服务器，零配置，文件即数据库
2. **懒写入**：首次写入时自动建表（已在初始化时完成建表）
3. **独立锚点**：每个数据库独立，一个损坏不影响其他
4. **可读可查**：所有数据可通过Python sqlite3直接查询

## 写脑时机（等待LLM激活）

- 每次LLM交互 → 自动提取lesson → lesson.db
- 每发现新事实 → facts.db  
- 每发现实体关联 → knowledge.db
- 每完成任务 → tasklog.db
- 定期淘汰低权重记忆 → forgotten.db
