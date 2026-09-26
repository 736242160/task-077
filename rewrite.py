#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rewrite.py — 带上下文限定、优先级与循环保护的文本重写工具（仅标准库）。

规则文件格式（每行一条，'|' 分隔字段；'#' 开头为注释，空行忽略）：

    @标记名 = 正则片段                                   # 命名标记定义（须在使用之前）
    规则名 | 匹配模式 | 上下文条件 | 替换文本 | prio=优先级   # 第 5 列可选

字段说明：
  - 匹配模式：Python 正则，可引用 @标记（如 第@数字+章）。
  - 上下文条件：逗号分隔的若干条件，留空或写 '-' 表示无条件。支持：
        前=X    匹配位置前面紧邻的文本须为 X
        后=X    匹配位置后面紧邻的文本须为 X
        前不=X  前面紧邻的文本须不为 X
        后不=X  后面紧邻的文本须不为 X
    X 为字面文本或 @标记；引用不存在的标记会按行报错。
  - 替换文本：支持 \\1、\\g<name>、\\g<0> 等反向引用；字段存在但为空表示删除。
  - prio=N：整数，缺省 0。同一位置多条规则同时匹配时，优先级高者先应用；
    优先级相同按定义顺序。

引擎语义：
  - 单轮内从左向右扫描；某位置完成替换后，扫描指针越过替换文本，
    替换结果不再参与该位置的再次匹配（防重叠），后续位置照常匹配。
  - 一轮结束后若发生过替换，则整体再扫一轮，直到某一轮无任何替换
    （收敛）或达到最大轮数。
  - 终止保护：默认最大 100 轮（--max-rounds 可调）。理由：替换结果在
    下一轮会重新参与匹配，形如「哈 -> 哈哈」的规则会让文本无限增长，
    任何此类循环都无法在引擎层面被静态判定，只能用轮数上限保证停机；
    100 轮对正常的级联重写（每轮推进一层）已绰绰有余，同时把失控规则
    的破坏限制在有界范围内。达到上限会输出告警并返回退出码 3。

用法：
    python3 rewrite.py 规则文件 < 输入.txt
    python3 rewrite.py 规则文件 -i 输入.txt -o 输出.txt --log 记录.txt
    python3 rewrite.py 规则文件 --max-rounds 5 < 输入.txt

退出码：0 正常收敛；2 规则文件有错；3 达到最大轮数被截断。
重写结果写标准输出（或 -o 文件），应用记录写标准错误（或 --log 文件）。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

DEFAULT_MAX_ROUNDS = 100

MARKER_DEF_RE = re.compile(r"^@(?P<name>\w+)\s*=\s*(?P<frag>.*)$")
MARKER_REF_RE = re.compile(r"@(?P<name>\w+)")
CONTEXT_ITEM_RE = re.compile(r"^(前不|后不|前|后)\s*=\s*(?P<value>.*)$")
PRIO_RE = re.compile(r"^(?:prio\s*=\s*)?(-?\d+)$")


@dataclass
class Cond:
    side: str
    negate: bool
    desc: str
    check: object


@dataclass
class Rule:
    name: str
    regex: re.Pattern
    conds: list
    replacement: str
    priority: int
    order: int
    lineno: int


@dataclass
class LogEntry:
    round_no: int
    pos: int
    rule: str
    matched: str
    replacement: str


def substitute_markers(text, markers, lineno, errors, where):
    def repl(m):
        name = m.group("name")
        if name not in markers:
            errors.append((lineno, f"{where}引用了不存在的标记 @{name}"))
            return ""
        return "(?:" + markers[name] + ")"

    return MARKER_REF_RE.sub(repl, text)


def build_cond(kind, value, markers, lineno, errors):
    side = kind[0]
    negate = len(kind) == 2
    desc = f"{side}{'不' if negate else ''}={value}"
    if value.startswith("@"):
        name = value[1:]
        if name not in markers:
            errors.append((lineno, f"上下文条件「{desc}」引用了不存在的标记 @{name}"))
            return None
        frag = markers[name]
        if side == "前":
            rx = re.compile(r"(?:%s)$" % frag)
            check = lambda t, s, e, rx=rx: rx.search(t[:s]) is not None
        else:
            rx = re.compile(frag)
            check = lambda t, s, e, rx=rx: rx.match(t, e) is not None
    else:
        lit = value
        if side == "前":
            check = lambda t, s, e, lit=lit: t[:s].endswith(lit)
        else:
            check = lambda t, s, e, lit=lit: t.startswith(lit, e)
    return Cond(side, negate, desc, check)


def parse_conds(field, markers, lineno, errors):
    conds = []
    field = field.strip()
    if not field or field == "-":
        return conds
    for item in re.split(r"[,，]", field):
        item = item.strip()
        if not item:
            continue
        m = CONTEXT_ITEM_RE.match(item)
        if not m:
            errors.append(
                (lineno, f"无法解析的上下文条件 {item!r}（支持 前= 后= 前不= 后不=）")
            )
            continue
        value = m.group("value").strip()
        if not value:
            errors.append((lineno, f"上下文条件 {item!r} 缺少比较值"))
            continue
        cond = build_cond(m.group(1), value, markers, lineno, errors)
        if cond is not None:
            conds.append(cond)
    return conds


def load_rules(path):
    errors = []
    rules = []
    markers = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.readlines()
    except OSError as exc:
        return None, [(0, f"无法读取规则文件: {exc}")]

    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        m = MARKER_DEF_RE.match(line)
        if m and line.startswith("@"):
            name = m.group("name")
            frag = m.group("frag").strip()
            if not frag:
                errors.append((lineno, f"标记 @{name} 缺少正则定义"))
                continue
            frag = substitute_markers(frag, markers, lineno, errors, f"标记 @{name} ")
            try:
                re.compile(frag)
            except re.error as exc:
                errors.append((lineno, f"标记 @{name} 的正则无效: {exc}"))
                continue
            markers[name] = frag
            continue

        fields = [f.strip() for f in line.split("|")]
        name = fields[0]
        if not name:
            errors.append((lineno, "缺少规则名"))
            continue
        if len(fields) < 2 or not fields[1]:
            errors.append((lineno, f"规则「{name}」缺少匹配模式"))
            continue
        if len(fields) < 4:
            errors.append(
                (lineno, f"规则「{name}」缺少替换文本（格式: 规则名 | 模式 | 上下文 | 替换文本）")
            )
            continue
        if len(fields) > 5:
            errors.append((lineno, f"规则「{name}」字段过多（最多: 名|模式|上下文|替换|prio=N）"))
            continue

        pattern_src = substitute_markers(fields[1], markers, lineno, errors, f"规则「{name}」的模式 ")
        try:
            regex = re.compile(pattern_src)
        except re.error as exc:
            errors.append((lineno, f"规则「{name}」的模式正则无效: {exc}"))
            continue

        conds = parse_conds(fields[2], markers, lineno, errors)

        priority = 0
        if len(fields) == 5 and fields[4]:
            pm = PRIO_RE.match(fields[4])
            if not pm:
                errors.append((lineno, f"规则「{name}」的优先级 {fields[4]!r} 无效（应为 prio=整数）"))
                continue
            priority = int(pm.group(1))

        rules.append(
            Rule(
                name=name,
                regex=regex,
                conds=conds,
                replacement=fields[3],
                priority=priority,
                order=len(rules),
                lineno=lineno,
            )
        )

    rules.sort(key=lambda r: (-r.priority, r.order))
    return rules, errors


def context_ok(rule, text, start, end):
    for cond in rule.conds:
        if cond.check(text, start, end) == cond.negate:
            return False
    return True


def apply_round(text, rules, log, round_no):
    out = []
    pos = 0
    changed = False
    n = len(text)
    while pos < n:
        hit = None
        for rule in rules:
            m = rule.regex.match(text, pos)
            if m is None:
                continue
            if context_ok(rule, text, pos, m.end()):
                hit = (rule, m)
                break
        if hit is None:
            out.append(text[pos])
            pos += 1
            continue
        rule, m = hit
        try:
            repl = m.expand(rule.replacement)
        except re.error as exc:
            raise SystemExit(f"规则「{rule.name}」替换文本无效: {exc}")
        out.append(repl)
        log.append(LogEntry(round_no, pos, rule.name, m.group(0), repl))
        changed = True
        if m.end() == pos:
            out.append(text[pos])
            pos += 1
        else:
            pos = m.end()
    return "".join(out), changed


def rewrite(text, rules, max_rounds):
    log = []
    rounds_used = 0
    converged = False
    for round_no in range(1, max_rounds + 1):
        rounds_used = round_no
        text, changed = apply_round(text, rules, log, round_no)
        if not changed:
            converged = True
            break
    return text, log, converged, rounds_used


def format_log(log, rounds_used, converged, max_rounds):
    lines = [f"=== 应用记录（共 {len(log)} 处替换，扫描 {rounds_used} 轮）==="]
    for e in log:
        lines.append(
            f"[第{e.round_no}轮] 位置{e.pos} 规则「{e.rule}」: {e.matched!r} -> {e.replacement!r}"
        )
    if converged:
        lines.append("终止状态: 已收敛（某一轮内未发生任何替换）")
    else:
        lines.append(
            f"终止状态: 达到最大轮数 {max_rounds} 后文本仍在变化，疑似循环规则，已强制截断"
        )
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="带上下文限定与优先级的文本重写工具（纯标准库）")
    ap.add_argument("rules", help="规则文件路径")
    ap.add_argument("-i", "--input", help="输入文本文件（缺省读标准输入）")
    ap.add_argument("-o", "--output", help="输出文件（缺省写标准输出）")
    ap.add_argument("--log", dest="log_path", help="应用记录输出文件（缺省写标准错误）")
    ap.add_argument(
        "--max-rounds",
        type=int,
        default=DEFAULT_MAX_ROUNDS,
        help="最大重写轮数，防循环终止保护（默认 %(default)s）",
    )
    args = ap.parse_args(argv)

    if args.max_rounds < 1:
        print("错误: --max-rounds 必须 >= 1", file=sys.stderr)
        return 2

    rules, errors = load_rules(args.rules)
    if errors:
        for lineno, msg in errors:
            where = f"第{lineno}行" if lineno else "规则文件"
            print(f"规则错误 {where}: {msg}", file=sys.stderr)
        return 2

    if args.input:
        with open(args.input, encoding="utf-8-sig") as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    result, log, converged, rounds_used = rewrite(text, rules, args.max_rounds)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result)
    else:
        sys.stdout.write(result)

    log_text = format_log(log, rounds_used, converged, args.max_rounds)
    if args.log_path:
        with open(args.log_path, "w", encoding="utf-8") as f:
            f.write(log_text + "\n")
    else:
        print(log_text, file=sys.stderr)

    return 0 if converged else 3


if __name__ == "__main__":
    sys.exit(main())
