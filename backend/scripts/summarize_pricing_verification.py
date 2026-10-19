"""Render a generated acceptance report without modifying business data."""
import argparse
from collections import Counter
import json
from pathlib import Path


def render(report):
    counts = Counter(item["status"] for item in report["items"])
    reused = sum(item.get("origin") == "existing_saved_estimate" for item in report["items"])
    causes = Counter(item.get("issue", {}).get("cause", "unspecified")
                     for item in report["items"] if item["status"] == "needs_input")
    rows = [
        "# 报价流程实单验收",
        "",
        f"订单 {report['job_id']}，输入版本 {report['input_revision']}。本报告为只读试算，未写回业务订单。",
        "",
        f"- 覆盖 {report['processed_items']} / {report['expected_items']} 项。",
        f"- 有待审核金额 {counts['priced_review']} 项，其中沿用已有估价 {reused} 项。",
        f"- 明确成本或资料问题 {counts['needs_input']} 项。",
        f"- 执行未完成 {counts['technical_failure']} 项。",
        "",
        "通过程序校验不代表费率、工艺解释和方案范围已经获得人工确认。"
        "未计价项不按零元处理，当前没有完整整单总价。",
        "",
        "## 待审核金额",
        "",
        "| 原序号 | 项目 | 金额（元） | 来源 |",
        "| --- | --- | ---: | --- |",
    ]
    for item in report["items"]:
        if item["status"] != "priced_review":
            continue
        source = "已有保存估价" if item.get("origin") == "existing_saved_estimate" else "新方案试算，待审核"
        rows.append(f"| {item.get('source_item', item['index'] + 1)} | "
                    f"{item['product'].replace('|', '/')} | {item['amount']} | {source} |")
    rows.extend(["", "## 问题分布", ""])
    labels = {"catalog_gap": "待核价", "customer_input": "待补充业务资料",
              "recognition_review": "待核对原文或图纸"}
    rows.extend(f"- {labels.get(cause, cause)}：{count} 项。" for cause, count in causes.items())
    rows.extend(["", "## 逐项问题", ""])
    for item in report["items"]:
        if item["status"] == "priced_review":
            continue
        issue = item.get("issue", {})
        rows.extend([
            f"### {item.get('source_item', item['index'] + 1)} · {item.get('product', '')}",
            "",
            f"状态：{labels.get(issue.get('cause'), item['status'])}。",
            "",
            issue.get("reason") or item.get("error") or "限定验收轮次内未形成有效结果，须查看执行记录。",
            "",
        ])
        if issue.get("question"):
            rows.extend(["待处理：" + issue["question"], ""])
    return "\n".join(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("output")
    args = parser.parse_args()
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(report), encoding="utf-8")
    print(str(output.resolve()))


if __name__ == "__main__":
    main()
