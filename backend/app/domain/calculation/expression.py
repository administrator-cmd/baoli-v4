"""计算节点的受限表达式引擎。

这一层只做一件事：把业务写在 Excel「计算公式」列里的算式，确定性地变成一棵
**受限 AST**，并在运行期只执行这棵树。

为什么不是 eval
---------------
`eval` / `exec` / `compile` / Excel 公式引擎都接受任意代码，而业务列里的算式来自
Excel 文件，属于**不可信输入**。这里从零写词法与语法分析，能构造出的节点只有四种
（数值、参数引用、一元负号、二元四则），任何函数调用、属性访问、下标、比较、赋值、
推导式在语法层就无法表达——不是靠黑名单拦，而是根本构造不出来。

设计边界
--------
- 只支持 `+ - * /` 与括号。函数集合**不预先实现**：真实样本出现哪个函数再按需加，
  并且加的时候要同时补上它的业务口径与单位规则。
- 全角运算符（÷ × − ＋ （））在词法阶段归一成半角：业务确实会写 `流动资产 ÷ 流动负债`。
- 标识符允许中文、字母、数字与下划线；「本期 / 上期 / 期初 / 期末」这类期间前缀
  由编译器从标识符里切出来，不在词法阶段处理（词法只认标识符边界）。
- 除零、缺失事实、精度与舍入都在求值层**显式**处理：抛 BusinessError 或返回明确的
  业务不可用状态，绝不静默取 0、绝不用 IFERROR 式吞错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Union

from ...errors import BusinessError


# 运算符与括号的全角形态。业务在 Excel 里写的是中文标点，不归一化会让整列公式
# 在词法阶段就全部报错——那不是「业务写错了」，是系统没认出来。
_OPERATOR_ALIASES = {
    "÷": "/",
    "／": "/",
    "×": "*",
    "＊": "*",
    "−": "-",
    "－": "-",
    "–": "-",
    "—": "-",
    "＋": "+",
    "（": "(",
    "）": ")",
    "·": "*",
    "＊": "*",
}

# 标识符：中文、字母、下划线开头，后接中文、字母、数字、下划线。
# 数字单独识别，因此标识符不允许以数字开头——`123` 与 `1abc` 必须区分开。
_IDENTIFIER = re.compile(r"[^\W\d][\w]*", re.UNICODE)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

_TOKEN_KINDS = ("NUMBER", "IDENT", "OP", "LPAREN", "RPAREN", "EOF")


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    position: int

    def describe(self) -> str:
        if self.kind == "EOF":
            return "算式结尾"
        return f"“{self.text}”"


class Node:
    """受限 AST 的基类。

    这里刻意不定义 `__call__` / `evaluate`：求值只在 evaluator 里做，
    节点本身只是数据，避免「顺手在节点上挂一段可执行逻辑」把受限性破坏掉。
    """

    __slots__ = ()


@dataclass(frozen=True)
class NumberLiteral(Node):
    value: Decimal


@dataclass(frozen=True)
class Identifier(Node):
    """一个参数引用原文，例如 `流动资产合计`、`本期营业收入`。"""

    name: str


@dataclass(frozen=True)
class Unary(Node):
    operator: str
    operand: Node


@dataclass(frozen=True)
class Binary(Node):
    operator: str
    left: Node
    right: Node


Expression = Union[NumberLiteral, Identifier, Unary, Binary]


def is_identifier(text: str) -> bool:
    """`text` 是否是一个**完整**的公式标识符。

    公式的标识符只能由中文、字母、下划线开头，后接中文、字母、数字、下划线组成；
    `-`、`+`、`*`、`/`、`(`、`)` 都是运算符，出现在标识符里会被切成两段
    （`P-001` 会被读成 `P 减 001`）。计算参数的编号要直接写进公式，因此必须满足这条约束——
    不符合时在配置期明确报出，而不是等到「公式没用到这个参数」这种看不出原因的错。
    """
    match = _IDENTIFIER.fullmatch(text.strip())
    return bool(match) and match.group(0) == text.strip()


def normalize_expression(text: str) -> str:
    """全角运算符归一成半角，并压缩空白。不做任何业务解释。"""
    normalized = text
    for source, target in _OPERATOR_ALIASES.items():
        normalized = normalized.replace(source, target)
    # 中文逗号/顿号在算式里没有意义，留着会让标识符粘连；显式报错而不是删掉。
    return normalized.strip()


def tokenize(text: str, *, location: str) -> list[Token]:
    """把算式切成记号。遇到无法识别的字符立即报错并指出位置。"""
    normalized = normalize_expression(text)
    tokens: list[Token] = []
    index = 0
    length = len(normalized)
    while index < length:
        char = normalized[index]
        if char.isspace():
            index += 1
            continue
        if char in "+-*/":
            tokens.append(Token("OP", char, index))
            index += 1
            continue
        if char == "(":
            tokens.append(Token("LPAREN", char, index))
            index += 1
            continue
        if char == ")":
            tokens.append(Token("RPAREN", char, index))
            index += 1
            continue
        number = _NUMBER.match(normalized, index)
        if number:
            tokens.append(Token("NUMBER", number.group(0), index))
            index = number.end()
            continue
        identifier = _IDENTIFIER.match(normalized, index)
        if identifier:
            tokens.append(Token("IDENT", identifier.group(0), index))
            index = identifier.end()
            continue
        raise BusinessError(
            "CALC_EXPRESSION_CHAR_UNSUPPORTED",
            f"{location}的算式里出现了当前不支持的字符“{char}”（第 {index + 1} 个字符）。"
            "首期只支持加、减、乘、除与括号；函数、百分比、比较符号与 Excel 公式一律不支持",
            details={"location": location, "expression": text, "char": char, "position": index},
        )
    tokens.append(Token("EOF", "", length))
    return tokens


class _Parser:
    """递归下降语法分析。优先级：括号 > 一元负号 > 乘除 > 加减。

    每个产生式都只产出上面四种节点之一，没有别的出口。
    """

    def __init__(self, tokens: list[Token], *, location: str, expression: str) -> None:
        self._tokens = tokens
        self._index = 0
        self._location = location
        self._expression = expression

    def _peek(self) -> Token:
        return self._tokens[self._index]

    def _advance(self) -> Token:
        token = self._tokens[self._index]
        self._index += 1
        return token

    def _error(self, message: str, token: Token | None = None) -> BusinessError:
        current = token or self._peek()
        return BusinessError(
            "CALC_EXPRESSION_SYNTAX_INVALID",
            f"{self._location}的算式“{self._expression}”在{current.describe()}处不成立：{message}",
            details={"location": self._location, "expression": self._expression, "position": current.position},
        )

    def parse(self) -> Expression:
        expression = self._parse_sum()
        if self._peek().kind != "EOF":
            raise self._error("算式结束后还有多余内容")
        return expression

    def _parse_sum(self) -> Expression:
        node = self._parse_product()
        while self._peek().kind == "OP" and self._peek().text in "+-":
            operator = self._advance().text
            node = Binary(operator=operator, left=node, right=self._parse_product())
        return node

    def _parse_product(self) -> Expression:
        node = self._parse_unary()
        while self._peek().kind == "OP" and self._peek().text in "*/":
            operator = self._advance().text
            node = Binary(operator=operator, left=node, right=self._parse_unary())
        return node

    def _parse_unary(self) -> Expression:
        if self._peek().kind == "OP" and self._peek().text == "-":
            self._advance()
            return Unary(operator="-", operand=self._parse_unary())
        if self._peek().kind == "OP" and self._peek().text == "+":
            # 一元正号没有语义，直接跳过；它不改变取值，因此不算静默兜底。
            self._advance()
            return self._parse_unary()
        return self._parse_atom()

    def _parse_atom(self) -> Expression:
        token = self._peek()
        if token.kind == "NUMBER":
            self._advance()
            try:
                value = Decimal(token.text)
            except InvalidOperation as exc:  # pragma: no cover - 正则已保证可解析
                raise self._error("数值无法解析", token) from exc
            return NumberLiteral(value=value)
        if token.kind == "IDENT":
            self._advance()
            # 标识符后紧跟左括号 = 函数调用。首期不支持任何函数，这里给出**专门**的
            # 提示而不是笼统的「语法错误」：业务看到「SUM 不支持」比看到
            # 「这里需要一个运算符」更容易明白该改什么。
            if self._peek().kind == "LPAREN":
                raise BusinessError(
                    "CALC_FUNCTION_UNSUPPORTED",
                    f"{self._location}的算式“{self._expression}”使用了函数“{token.text}( )”。"
                    "当前版本不支持任何函数，只支持加、减、乘、除与括号；"
                    "函数会在真实样本需要时按业务口径逐个接入",
                    details={
                        "location": self._location,
                        "expression": self._expression,
                        "function": token.text,
                        "position": token.position,
                    },
                )
            return Identifier(name=token.text)
        if token.kind == "LPAREN":
            self._advance()
            node = self._parse_sum()
            if self._peek().kind != "RPAREN":
                raise self._error("括号没有闭合")
            self._advance()
            return node
        raise self._error("这里需要一个数值、一个参数引用或一个左括号")


def parse_expression(text: str, *, location: str) -> Expression:
    """解析算式文本，返回受限 AST。

    `location` 是给人看的定位串（工作表 + 行号 + 列名），用于把语法错误指回原格。
    空算式在这里就报错：调用方必须先判断该行是否配置了算式，不能拿空串进来。
    """
    if not isinstance(text, str) or not text.strip():
        raise BusinessError(
            "CALC_EXPRESSION_EMPTY",
            f"{location}的计算公式为空，请填写算式后重新上传",
            details={"location": location},
        )
    return _Parser(tokenize(text, location=location), location=location, expression=text).parse()


def walk(node: Expression) -> list[Node]:
    """按书写顺序展开 AST 的全部节点，供编译期收集参数引用与校验使用。"""
    result: list[Node] = [node]
    if isinstance(node, Unary):
        result.extend(walk(node.operand))
    elif isinstance(node, Binary):
        result.extend(walk(node.left))
        result.extend(walk(node.right))
    return result


def identifiers(node: Expression) -> list[str]:
    """AST 里出现的全部参数引用原文，去重并保持首次出现顺序。"""
    seen: list[str] = []
    for item in walk(node):
        if isinstance(item, Identifier) and item.name not in seen:
            seen.append(item.name)
    return seen


def ast_to_payload(node: Expression) -> dict[str, Any]:
    """AST 的可 JSON 化形式，用于写进 manifest 冻结下来。

    已发布的 manifest 必须能脱离原始 Excel 独立解释，因此编译结果要能落库；
    运行期只读这份结构，不再重新解析原文本。
    """
    if isinstance(node, NumberLiteral):
        return {"kind": "NUMBER", "value": format(node.value, "f")}
    if isinstance(node, Identifier):
        return {"kind": "IDENTIFIER", "name": node.name}
    if isinstance(node, Unary):
        return {"kind": "UNARY", "operator": node.operator, "operand": ast_to_payload(node.operand)}
    if isinstance(node, Binary):
        return {
            "kind": "BINARY",
            "operator": node.operator,
            "left": ast_to_payload(node.left),
            "right": ast_to_payload(node.right),
        }
    raise BusinessError("CALC_AST_NODE_UNKNOWN", "算式编译结果包含未知节点类型", status_code=500)
