"""知识图谱抽取（产品自研）：从切片里抽**有意义的实体**，而不是关键词云。

设计要点（针对"图上一堆代码标识、标签糊成一团"的问题）：
  * 噪声过滤：丢掉代码标识（含下划线/比较符/纯英文碎片/单字），只留中文词、装备代号、
    章节号、参数名与资料名；
  * 实体分类：资料 / 代号 / 章节 / 参数 / 部件 / 术语 —— 前端按类别着色与过滤；
  * 权重可解释：weight = 出现该实体的切片数（并给出覆盖率），按权重截断到 topN，
    保证图上每个点都能看清、都有意义；
  * 关系：同一切片中共现 → 边；同一文档内共现的关系标注为"同文档"，跨文档标注为"跨资料"。
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .lexicon import Lexicon

#: 过于泛化、做实体没有信息量的词
GENERIC = {
    "数据", "数据表", "数据类型", "数据项", "数据库", "信息", "系统", "功能", "模块", "文件", "文档",
    "内容", "说明", "要求", "记录", "方式", "名称", "编号", "版本", "单位", "时间", "数量", "结果",
    "输入", "输出", "输入输出", "参数", "配置", "值", "字段", "类型", "状态", "列表", "表格", "序号",
    "项目", "对象", "操作", "流程", "步骤", "方法", "条件", "平台", "接口", "服务", "资源", "格式",
    "异常", "错误", "提示", "用户", "管理", "测试", "开发", "设计", "需求", "计划", "报告",
    # 时间/数量/泛用动词与连接词：在实体图里没有信息量
    "一次", "一月", "个月", "小时", "分钟", "每天", "每年", "当天", "日期", "时候", "情况",
    "使用", "前提", "检查", "记录", "更换", "补充", "出现", "必要", "进行", "以及", "存在",
    "相关", "一般", "必须", "规定", "按照", "根据", "如果", "否则", "并且", "或者", "包括",
    "例如", "如下", "上述", "是否", "可能", "需要", "应该", "可以", "不得", "严禁", "确保",
    "工作", "作业", "人员", "部门", "完成", "实现", "采用", "具有", "主要", "基本", "相应",
    # 研制/软件文档里的通用词：2026-09-28 混库实测，这些词在单份文档里出现几百次，
    # 按"覆盖资料数"也照样霸榜，把装备域实体（液压/溢流阀/泵站）挤出 top-N
    "信息库", "流水号", "字段名", "优先级", "中文名", "中文名称", "英文名", "英文名称",
    "客户端", "服务端", "管理系统", "源代码", "错误码", "段长度", "关键字", "关键词", "归一化",
    "模型库", "历史数据", "数据流", "工作量", "关系数据库", "标准化", "前置条件", "准确性",
    "单元测试", "运行日志", "实验员", "默认值", "状态栏", "一致性", "唯一性", "完整性",
    "一一对应", "统计分析", "系统生成", "数据交换", "注意事项", "指标体系", "系统日志",
    "保障系统", "配置项", "返回值", "调用参数", "菜单", "按钮", "页面", "账号", "权限",
    "模块名", "功能模块", "权值", "权重", "状态位", "标志位", "序列化", "反序列化",
    "接口文档", "测试用例", "测试报告", "需求文档", "设计文档", "操作手册", "用户手册",
    # 文档元信息：版本台账/研制文档的表头与模板话术，任何语料里都不该当实体
    "版本号", "有效期", "负责人", "可追溯", "适用范围", "保持一致", "管理员", "系统管理员",
    "二进制", "元数据", "分析报告", "可视化", "实验设计", "报文格式", "版本升级", "资源类型",
    "签署", "审核", "批准", "页眉", "页脚", "目录", "附录", "尾注", "版本记录", "修订记录",
    "评审意见", "参会人员", "会议记录", "工作计划", "工作总结", "报告模板", "指标项",
    "指标值", "指标定义", "统计口径", "计算公式", "取值范围", "说明栏",
    # 文档结构词与时间泛词：出现频次高但没有任何实体信息（2026-09-28 提高抽取率后实测）
    "缩略语", "对照表", "长时间", "周期性", "全过程", "必要时", "上表", "下表", "该表",
    "本表", "本节", "本章", "上述内容", "相关内容", "具体情况", "实际情况", "不同", "不能",
    "不得", "不需", "不应", "不会", "无需", "并且", "应检查", "应使用", "应当在",
    "技术参数", "系统结构", "作业时间", "工作时间", "连续作业", "环境条件", "注意事项",
    "本规程", "本文件", "本手册", "本章节", "该规程",
}
#: 中文实体里不该出现的**功能字**（虚词/介词/连词/助词/能愿动词）。
#: 实测反例：油位低于液位 / 液压油至液位 / 将系统压力 / 与举升油缸 / 待油温。
#: 注意刻意**不含**：不（不锈钢）、过（过滤器）、并（并联）、应（响应）、对（对照表）。
STOP_CHARS = set("的着了和与及或且而为在把将被由从是之其所就也还才只没待至于需须要使以经后中若如但")
#: 只禁止出现在**开头**的功能字：这些字能当动词/介词起头（时溢流阀 / 向电磁换向阀 /
#: 并对油缸 / 查滤芯 / 测溢流阀 / 按额定工作压力），但出现在词内是合法的
#: （换向阀 / 时间 / 中间 / 同时），所以不能放进 STOP_CHARS。
LEAD_STOP = set("时向并应可方再按当对查测且如若但个各该此其")
#: 以动词开头的候选是"动作"不是"实体"（实测：检查滤芯 / 更换密封件 / 检测溢流阀 / 维持系统压力）。
#: 刻意不含 开启（开启压力）、调节（调节阀）、启动（启动压力）。
#: 候选词里**任何位置**都不该出现的动词（"动作"不是实体）。
#: 只用在开头查是不够的：实测残留的是"动宾短语"，动词在中间或第 3 个字
#: （方可检查溢流阀 / 堵塞时更换滤芯 / 监听液压泵站 / 油缸回油经换向阀 / 保压期间压力）。
#: 词典词条（参数表/部件表）受保护，不受这条规则影响——"滤芯更换周期""油温停机检查阈值"
#: 这类含动词的规范参数名照常入图。
INNER_VERB = ("检查", "更换", "检测", "检修", "检验", "确认", "观察", "维持", "清洗",
              "补充", "安装", "拆卸", "拆除", "确保", "进行", "采用", "记录", "打开",
              "关闭", "拧紧", "松动", "排放", "调整", "处理", "复位", "停止", "启动",
              "操作", "监听", "驱动", "取出", "装入", "另设", "清理", "靠近", "符合",
              "输出", "提供", "减小", "吸收", "采集", "限制", "限定", "具备", "产生",
              "返回", "堵塞", "弯曲", "补加", "加注", "继续", "保持", "出现", "发生",
              "超过", "大于", "小于", "低于", "高于", "达到", "避免", "防止", "期间")

#: 中文实体最大字数：装备域术语（溢流阀开启压力 / 蓄能器充氮压力）实测最长 7 字
MAX_TERM = 8
CODE_HINT = re.compile(r"[_<>=(){};]|^[a-z0-9_.]+$")
CN_WORD = re.compile(r"^[\u4e00-\u9fff]{2,8}$")
CN_MIX = re.compile(r"^[\u4e00-\u9fff]{2,12}$")
CODENAME = re.compile(r"^[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+$")   # HYS-WX-003 / JJ-006 / L-HM46
STANDARD = re.compile(r"^[A-Za-z]+\s?\d{3,}$")
CLAUSE = re.compile(r"^\d+(?:\.\d+){1,3}$")
PARAM_SUFFIX = ("压力", "温度", "周期", "时长", "时间", "液位", "等级", "扭矩", "转速", "流量", "容量", "浓度", "清洁度", "油温", "行程")
PART_SUFFIX = ("泵站", "滤芯", "蓄能器", "油缸", "阀", "电控单元", "控制器", "传感器", "软管", "密封件", "冷却器", "油箱")
DOC_SUFFIX = (".docx", ".pdf", ".xlsx", ".md", ".txt", ".doc", ".xls")


#: 版本节点（"HYS-CZ-002 V4.0"）：由词典从版本台账生成，代表某个资料的某一版
VERSION_NODE = re.compile(r"^[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+ V?\d+(?:\.\d+){1,2}$")


def classify(term: str) -> str:
    if term.lower().endswith(DOC_SUFFIX):
        return "资料"
    if VERSION_NODE.match(term):
        return "版本"
    if CLAUSE.match(term):
        return "章节"
    if CODENAME.match(term) or STANDARD.match(term):
        return "代号"
    if term.endswith(PARAM_SUFFIX):
        return "参数"
    if term.endswith(PART_SUFFIX):
        return "部件"
    return "术语"


def is_noise(term: str) -> bool:
    """判定噪声：代码标识、英文碎片、单字、长度异常、纯数字、章节/版本/数值碎片、泛化词。"""
    if not term or len(term) < 2 or len(term) > 16:
        return True
    if term in GENERIC:
        return True
    # 章节号与裸数值（3.1.4 / 2.2.1）以及版本号、参数值（V3.0 / 2.0 / 17.5 / 0.05）在图谱里
    # 没有信息量，还会与真实代号（HYD-RV-04）和参数实体名（额定工作压力）混淆，故按噪声处理。
    # 判据见 docs/16 §7「图谱可读性整改」，日期 2026-09-28。
    if CLAUSE.match(term):
        return True
    if term.isdigit():
        return True
    if CODE_HINT.search(term):
        return False if (CODENAME.match(term) or STANDARD.match(term)) else True
    if CN_WORD.match(term) or CN_MIX.match(term):
        # 纯中文候选再做两道构词过滤：夹带功能字、以动词开头都不是实体
        # （判据见 docs/16 §7「实体抽取率与碎片剪枝」，2026-09-28）
        if set(term) & STOP_CHARS or term[0] in LEAD_STOP:
            return True
        if any(verb in term for verb in INNER_VERB):
            return True
        # 中文实体至少要 3 个字，除非它本身就是领域部件/参数词（滤芯 / 溢流阀 / 充氮压力）
        if len(term) >= 3 or term.endswith(PART_SUFFIX + PARAM_SUFFIX):
            return False
        return True
    if CODENAME.match(term) or STANDARD.match(term):
        return False
    # 纯英文词：只保留长度 ≥4 的全大写缩写（如 NAS、SQL 除外）
    if re.fullmatch(r"[A-Za-z]+", term):
        return not (term.isupper() and len(term) >= 3)
    return True


def _candidates(chunk: Dict[str, Any], evidence: Optional[List[str]] = None) -> List[str]:
    """一个切片里的候选实体。

    三个来源，互补：
      1. 知识内核（RAGFlow 分词器）产出的 tokens —— 中文按词切分，最可靠；
      2. 正文里的结构化标记：装备代号（HYS-WX-003 / L-HM46）与章节号（3.1.2）；
      3. 以领域后缀结尾的中文词块（…泵站 / …滤芯 / …压力 / …周期），兜住分词切碎的术语。
    """
    terms: List[str] = []
    for word in str(chunk.get("tokens") or "").split():
        terms.append(word.strip())
    for word in chunk.get("important_keywords") or []:
        terms.append(str(word).strip())
    text = str(chunk.get("content") or "")
    terms.extend(re.findall(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+", text))
    terms.extend(re.findall(r"\d+(?:\.\d+){1,3}(?=\s|$|[，。；])", text))
    # 中文长串不再按 12 字切断（切了就丢字），窗口长度由 MAX_TERM 统一约束。
    for match in re.finditer(r"[\u4e00-\u9fff]{2,}", text):
        piece = match.group(0)
        for suffix in PART_SUFFIX + PARAM_SUFFIX:
            index = piece.find(suffix)
            while index >= 0:
                end = index + len(suffix)
                terms.append(suffix)
                # **逐字向左扩展**，把"…压力 / …周期 / …泵站"的各种长度窗口都产出来。
                # 以前只取固定 4 字窗口，左边的字被硬切掉，于是出现"流阀开启压力"
                # （丢了溢）、"能器充氮压力"（丢了蓄）这类碎片。
                # 多产出的碎片由 _collapse_contained 按"包含关系 + 覆盖数"剪掉。
                for length in range(min(MAX_TERM, end), len(suffix), -1):
                    terms.append(piece[end - length:end])
                # 再往左多取几个字的窗口只作**剪枝证据**（不进图）：
                # 用来判定"系统额定工作压力"这类从更长串里切出来的左残片。
                if evidence is not None:
                    for length in range(min(MAX_TERM + 3, end), MAX_TERM, -1):
                        evidence.append(piece[end - length:end])
                index = piece.find(suffix, index + 1)
    return terms


def _node(term: str, count: int, occurrences: int, total_docs: int = 1,
          lexicon: Optional[Lexicon] = None, occurrences_override: Optional[int] = None) -> Dict[str, Any]:
    """构造实体节点。

    count = **覆盖的资料份数**（排序与"关联强度"都用它）；
    occurrences = 在多少个切片里出现过（仅作参考值，体现词频）。
    """
    category = ""
    if lexicon is not None:
        category = lexicon.categories.get(term, "")
    return {
        "id": term,
        "name": term,
        "category": category or classify(term),
        "value": count,
        "weight": count,
        "coverage": round(count / max(total_docs, 1), 4),
        "occurrences": occurrences if occurrences_override is None else occurrences_override,
    }


def extract_entities(chunks: List[Dict[str, Any]], top_n: int = 80,
                     min_chunks: int = 1, lexicon: Optional[Lexicon] = None) -> Tuple[List[Dict[str, Any]], int]:
    """返回 (实体列表, 被过滤的噪声数)。

    传入 lexicon 时：词典词条按"出现在多少个切片里"计入，并在截断前**优先保留**——
    这些是语料自带对照表/台账里的权威实体（部件、代号、资料编号、版本、缩略语），
    不该因为分词器切碎或权重排名靠后而丢掉。
    """
    chunk_count: Dict[str, int] = defaultdict(int)
    docs_count: Dict[str, Set[str]] = defaultdict(set)
    domain_docs_count: Dict[str, Set[str]] = defaultdict(set)
    total: Dict[str, int] = defaultdict(int)
    noise = 0

    # **领域语料基准**：混库（同一知识库里既有装备资料又有研制文档）时，
    # 按全库算覆盖度会让"在每份软件文档里都出现的模块代号"霸榜。
    # 以语料自带的《资料清单与版本台账》识别领域资料（正文里出现其资料编号的文档），
    # 排名只看实体在这些领域资料中的覆盖度（判据见 docs/16 §7 D31）。
    domain_codes = set(lexicon.documents) if lexicon is not None else set()
    domain_docs = {str(c.get("doc_name") or c.get("document_id") or "")
                   for c in chunks
                   if any(code in str(c.get("content") or "") for code in domain_codes)}
    domain_total = max(len(domain_docs), 1)
    #: 候选词的**原始**词频（含被 is_noise 挡掉的、以及超长证据窗口）。
    #: 只用来判断"某个词是不是从更长串里切出来的左残片"，不参与排名。
    raw_count: Dict[str, int] = defaultdict(int)
    for chunk in chunks:
        text = str(chunk.get("content") or "")
        document = str(chunk.get("doc_name") or chunk.get("document_id") or "")
        seen: Set[str] = set()
        evidence: List[str] = []
        candidates = _candidates(chunk, evidence)
        for term in candidates + evidence:
            raw_count[term.strip().strip("，。；：、（）()[]【】")] += 1
        for term in candidates:
            term = term.strip().strip("，。；：、（）()[]【】")
            if is_noise(term):
                noise += 1
                continue
            total[term] += 1
            if term not in seen:
                seen.add(term)
                chunk_count[term] += 1
            docs_count[term].add(document)
            if document in domain_docs:
                domain_docs_count[term].add(document)
        if lexicon is not None and lexicon.terms:
            for term in lexicon.terms:
                if len(term) <= 4:
                    # 短词（PU / RV / AC）要按词边界匹配：否则它们会命中 HYD-PU-01 这类代号里的
                    # 子串，把"覆盖资料数"虚高（实测 PU 曾因此翻到 13 份）
                    hit = bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])",
                                             text))
                else:
                    hit = term in text
                if hit:
                    total[term] += text.count(term) if len(term) > 4 else 1
                    if term not in seen:
                        seen.add(term)
                        chunk_count[term] += 1
                    docs_count[term].add(document)
                    if document in domain_docs:
                        domain_docs_count[term].add(document)
    nodes: List[Dict[str, Any]] = []
    docs_total = max(1, len({str(c.get("doc_name") or c.get("document_id") or "") for c in chunks}))
    rank_total = domain_total if domain_codes else docs_total
    lexicon_terms = set(lexicon.terms) if lexicon is not None else set()
    for term, count in chunk_count.items():
        if count < min_chunks:
            continue
        # 长度 ≥4 的长词只出现一次，几乎都是长句切出来的偶发短语
        # （实测：连续监看油温 / 泵站发出周期 / 同时给出可执行程）。短词不受此限。
        if len(term) >= 4 and count < 2 and term not in lexicon_terms:
            continue
        # 排序权重 = **领域资料覆盖数**（混库时只数领域资料）；
        # 有领域语料时就只看它——这里刻意**不**给全库覆盖兜底，
        # 否则"只在研制文档里出现"的实体会借着全库数量挤回前排（2026-09-28 实测踩到）。
        # 没有领域语料时退回全库资料数。词频（occurrences）只作参考，不参与排名。
        rank = len(domain_docs_count.get(term, ())) if domain_codes else len(docs_count.get(term, ()))
        nodes.append(_node(term, rank, total[term], rank_total, lexicon,
                         occurrences_override=count))
    nodes.sort(key=lambda item: (-item["value"], -item["occurrences"], item["name"]))
    nodes = _drop_bare_suffixes(nodes)
    nodes = _collapse_contained(nodes, protected=set(lexicon.terms) if lexicon is not None else set(),
                                raw_count=raw_count)
    if lexicon is None or not lexicon.terms:
        return nodes[:top_n], noise
    # 词典实体**先占名额**，再用共现实体把剩余名额按权重补满。
    # 关键点：判定"已在图里"必须针对**截断之后**的结果——版本节点（"HYS-CZ-002 V4.0"）
    # 在正文里不是连续字符串、权重也低，按权重永远进不了 topN，但它承载"现行/作废"
    # 这个最关键的事实，必须钉住（判据见 docs/16 §7 D29）。
    pinned = [_node(term, len(domain_docs_count.get(term, ())) or len(docs_count.get(term, ()))
                         or chunk_count.get(term, 1),
                    total.get(term, 1), rank_total, lexicon,
                    occurrences_override=chunk_count.get(term, 1))
              for term in sorted(lexicon.terms)]
    pinned_names = {item["name"] for item in pinned}
    room = max(top_n - len(pinned), 0)
    # **类别配额**填充：混库场景下模块代号（ASM-M01…）会出现在每一份软件文档里，
    # 单靠排序或停用词都挡不住，于是给"代号/术语"两类设名额上限，
    # 把机会留给部件/参数/资料/版本这些领域实体（2026-09-28 混库实测后调整）。
    quota = {"代号": 10, "术语": 10, "部件": 999, "参数": 999, "缩略语": 999, "资料": 999,
             "版本": 999}
    default_quota = 6
    used_per_cat: Dict[str, int] = defaultdict(int)
    rest: List[Dict[str, Any]] = []
    for item in nodes:
        if len(rest) >= room:
            break
        if item["name"] in pinned_names:
            continue
        category = item["category"]
        if used_per_cat[category] >= quota.get(category, default_quota):
            continue
        used_per_cat[category] += 1
        rest.append(item)
    return pinned + rest, noise


def _drop_bare_suffixes(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """去掉「光秃秃的后缀词」：当 泵站 / 压力 / 周期 这类后缀已有更长的实体时不再单独成点。"""
    bare = set(PART_SUFFIX) | set(PARAM_SUFFIX)
    names = [node["name"] for node in nodes]
    kept: List[Dict[str, Any]] = []
    for node in nodes:
        if node["name"] in bare and any(node["name"] != other and node["name"] in other for other in names):
            continue
        kept.append(node)
    return kept


CN_ONLY = re.compile(r"^[\u4e00-\u9fff]+$")


def _left_clip_support(raw_count: Dict[str, int]) -> Dict[str, int]:
    """统计"每个词左边再多一个字"的总词频：support[A] = Σ freq(c + A)。

    这是判断左残片的直接证据。中文没有空格，固定宽度窗口必然会在词中间下刀，
    切出来的碎片有个共同特征：**它从来不自己起头**，前面总还有字
    （实测：查周期 = 检查周期 + 核查周期、测溢流阀 = 检测溢流阀 + 再测溢流阀、
    换周期 = 更换周期 + 交换周期、察压力 ← 观察压力、经滤芯 ← 油经滤芯）。

    注意必须取**和**而不是最大值：碎片常常由好几个不同的长词各切出一份
    （查周期 来自 检查周期 与 核查周期 两个词），取最大值会漏判。
    """
    support: Dict[str, int] = defaultdict(int)
    for term, count in raw_count.items():
        if len(term) >= 3 and CN_ONLY.match(term):
            support[term[1:]] += count
    return support


def _collapse_contained(nodes: List[Dict[str, Any]], protected: Optional[Set[str]] = None,
                        raw_count: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
    """剪掉"左一个字被别人吃掉"的残片（判据见上）。

    只在 support(A) ≥ freq(A) 时判定为残片——即 A 出现多少次，左边的窗口就出现至少多少次，
    A 从来没有独立成词过。反过来 support(A) < freq(A) 说明 A 有很多独立出现（额定工作压力
    虽然常出现在"液压系统额定工作压力"里，但它自己出现得更多），必须保留。

    两条例外，避免误杀核心概念：
      * 词典词条（权威实体：参数表/部件表/台账/对照表）永不参与；
      * 2 字且不以部件/参数后缀结尾的词（液压 / 油缸）保留——它们是领域根概念。
    """
    if not raw_count:
        return nodes
    guard = protected or set()
    suffixes = PART_SUFFIX + PARAM_SUFFIX
    support = _left_clip_support(raw_count)
    kept: List[Dict[str, Any]] = []
    for node in sorted(nodes, key=lambda item: (-item["value"], -len(item["name"]), item["name"])):
        name = node["name"]
        if name in guard or not CN_ONLY.match(name):
            kept.append(node)
            continue
        # 2 字且**不以部件/参数后缀结尾**的词是领域根概念（液压 / 油缸），保留；
        # 以"阀 / 油缸 / 压力"结尾的 2 字词（流阀 / 向阀）则是被吃掉一个字的残片，照剪。
        if len(name) <= 2 and not name.endswith(suffixes):
            kept.append(node)
            continue
        if support.get(name, 0) >= raw_count.get(name, 0):
            continue
        kept.append(node)
    return kept


def build_graph_entities(chunks: List[Dict[str, Any]], top_n: int = 400,
                         max_edges: int = 800,
                         lexicon: Optional[Lexicon] = None,
                         per_node: int = 6) -> Dict[str, Any]:
    """按切片共现建图 + 叠加词典关系，返回 {nodes, edges, stats}。

    两类边：
      * 共现边（同文档 / 跨资料）—— 由切片共现统计得到，参与稀疏化；
      * 词典边（代号 / 含义 / 现行版本 / 已作废）—— 来自语料自带的对照表与版本台账，
        **钉住不参与稀疏化**，因为它们是权威事实，稀疏化掉就等于把版本陷阱弄丢了。
    """
    nodes, noise = extract_entities(chunks, top_n=top_n, lexicon=lexicon)
    keep = {node["id"] for node in nodes}
    doc_of: Dict[str, Set[str]] = defaultdict(set)
    pair_chunks: Dict[Tuple[str, str], int] = defaultdict(int)
    pair_docs: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    for chunk in chunks:
        found: List[str] = []
        for term in _candidates(chunk):
            term = term.strip().strip("，。；：、（）()[]【】")
            if term in keep and term not in found:
                found.append(term)
        if lexicon is not None:
            text = str(chunk.get("content") or "")
            for term in keep:
                if term not in found and term in text:
                    found.append(term)
        document = str(chunk.get("doc_name") or chunk.get("document_id") or "")
        for term in found:
            doc_of[term].add(document)
        for index, left in enumerate(found):
            for right in found[index + 1:]:
                key = tuple(sorted((left, right)))
                pair_chunks[key] += 1
                pair_docs[key].add(document)
    edges: List[Dict[str, Any]] = []
    for (left, right), count in pair_chunks.items():
        documents = pair_docs[(left, right)]
        edges.append({
            "source": left,
            "target": right,
            "relation": "同文档" if len(documents) <= 1 else "跨资料",
            "weight": count,
            "documents": len(documents),
        })
    edges.sort(key=lambda item: (-item["weight"], -item["documents"]))
    edges = _thin_edges(edges, len(nodes), max_edges, per_node=per_node)
    result = _finish(nodes, edges, noise, len(chunks), candidate_total=len(keep))
    if lexicon is not None and lexicon.edges:
        # 词典边优先于共现边：同一对实体如果词典已经给了确切关系（代号/现行版本/已作废/含义），
        # 就用词典的关系名，而不是笼统的"同文档/跨资料"。
        pinned: List[Dict[str, Any]] = []
        taken: Set[Tuple[str, str]] = set()
        for edge in lexicon.edges:
            if edge["source"] not in keep or edge["target"] not in keep:
                continue
            key = tuple(sorted((edge["source"], edge["target"])))
            if key in taken:
                continue
            taken.add(key)
            pinned.append(dict(edge))
        kept_edges = [edge for edge in edges
                      if tuple(sorted((edge["source"], edge["target"]))) not in taken]
        result["edges"] = pinned + kept_edges
        result["stats"]["edge_count"] = len(result["edges"])
        result["stats"]["lexicon_edges"] = len(pinned)
        relation_counts: Dict[str, int] = defaultdict(int)
        for edge in result["edges"]:
            relation_counts[edge["relation"]] += 1
        result["stats"]["relations"] = dict(relation_counts)
    if lexicon is not None:
        result["stats"]["lexicon"] = dict(lexicon.stats)
    return result


def _thin_edges(edges: List[Dict[str, Any]], node_count: int, max_edges: int, per_node: int = 6) -> List[Dict[str, Any]]:
    """每个实体保留最强的前 N 条边。

    原来取 3 太狠：80 个实体只剩 127 条边（实测），把图谱抽干成稀疏网。
    改为 6，并允许调用方覆盖（2026-09-28 甲方反馈"抽取率太低"）。
    """
    degree: Dict[str, int] = defaultdict(int)
    kept: List[Dict[str, Any]] = []
    for edge in edges:
        if len(kept) >= max_edges:
            break
        left, right = edge["source"], edge["target"]
        if degree[left] >= per_node or degree[right] >= per_node:
            continue
        degree[left] += 1
        degree[right] += 1
        kept.append(edge)
    return kept


def _finish(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]], noise: int,
            chunk_total: int, candidate_total: int = 0) -> Dict[str, Any]:
    categories: Dict[str, int] = defaultdict(int)
    for node in nodes:
        categories[node["category"]] += 1
    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "categories": dict(categories),
            "noise_filtered": noise,
            "chunks": chunk_total,
            # 抽取率口径：切片 → 候选词 → 入图实体 → 关系
            "candidates": candidate_total,
            "entity_rate": round(len(nodes) / max(chunk_total, 1), 4),
            "edge_rate": round(len(edges) / max(chunk_total, 1), 4),
        },
    }


# ---------------------------------------------------------------- A3 嵌入归并
def merge_similar_entities(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]],
                           gateway: Any, threshold: float = 0.95,
                           protected: Optional[Set[str]] = None
                           ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """用**嵌入模型**（如 bge-m3）把近义实体并成一个：同义、全称/简称这类。

    只用嵌入算余弦相似度，不调用任何生成式大模型；阈值取高（默认 0.95）——宁可漏并、不可错并；
    词典里的权威实体（代号/资料/版本/缩略语）受保护，绝不参与合并。
    """
    if gateway is None or len(nodes) < 2:
        return nodes, edges, {"merged": 0}
    names = [node["name"] for node in nodes]
    try:
        vectors = gateway.embed(names, batch_size=16, use_cache=True)
    except Exception as exc:  # noqa: BLE001 —— 嵌入不可用就跳过归并，不影响出图
        return nodes, edges, {"merged": 0, "error": str(exc)[:160]}
    if len(vectors) != len(names):
        return nodes, edges, {"merged": 0, "error": "嵌入数量不匹配"}
    guard = protected or set()

    def cosine(left: Any, right: Any) -> float:
        dot = sum(a * b for a, b in zip(left, right))
        norm_left = sum(a * a for a in left) ** 0.5
        norm_right = sum(b * b for b in right) ** 0.5
        if not norm_left or not norm_right:
            return 0.0
        return dot / (norm_left * norm_right)

    parent: Dict[str, str] = {node["id"]: node["id"] for node in nodes}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    pairs: List[List[Any]] = []
    for i in range(len(nodes)):
        if names[i] in guard:
            continue
        for j in range(i + 1, len(nodes)):
            if names[j] in guard:
                continue
            if find(names[i]) == find(names[j]):
                continue
            score = cosine(vectors[i], vectors[j])
            if score >= threshold:
                parent[find(names[j])] = find(names[i])
                pairs.append([names[i], names[j], round(score, 4)])
    if not pairs:
        return nodes, edges, {"merged": 0}

    weight = {node["id"]: float(node.get("value") or node.get("weight") or 0) for node in nodes}
    canonical: Dict[str, str] = {}
    for node in nodes:
        root = find(node["id"])
        current = canonical.get(root)
        if current is None or (weight[node["id"]], len(node["id"])) > (weight[current], len(current)):
            canonical[root] = node["id"]
    alias_of = {node["id"]: canonical[find(node["id"])] for node in nodes}

    merged_nodes: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        target = alias_of[node["id"]]
        bucket = merged_nodes.setdefault(target, {**node, "aliases": []})
        if node["id"] != target:
            bucket["value"] = max(float(bucket.get("value") or 0), weight[node["id"]])
            bucket["weight"] = bucket["value"]
            bucket["occurrences"] = int(bucket.get("occurrences") or 0) + int(node.get("occurrences") or 0)
            if node["name"] not in bucket["aliases"]:
                bucket["aliases"].append(node["name"])

    deduped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for edge in edges:
        source, target = alias_of.get(edge["source"], edge["source"]), alias_of.get(edge["target"], edge["target"])
        if source == target:
            continue
        item = {**edge, "source": source, "target": target}
        key = (source, target)
        current = deduped.get(key)
        if current is None or float(item.get("weight") or 0) > float(current.get("weight") or 0):
            deduped[key] = item
    return list(merged_nodes.values()), list(deduped.values()), {
        "merged": len(nodes) - len(merged_nodes), "pairs": pairs[:20]}


# ---------------------------------------------------------------- A4 社区发现
def detect_communities(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]],
                       max_iter: int = 40) -> Dict[str, str]:
    """标签传播社区发现（纯图算法：确定性、无随机、不依赖任何模型）。

    返回 {实体 id: 社区号}，社区按规模从大到小编为 C01、C02…，前端据此着色与聚合。
    """
    label = {node["id"]: node["id"] for node in nodes}
    adjacency: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for edge in edges:
        source, target = edge["source"], edge["target"]
        if source not in label or target not in label or source == target:
            continue
        weight = float(edge.get("weight") or 1)
        adjacency[source].append((target, weight))
        adjacency[target].append((source, weight))
    for _ in range(max_iter):
        changed = False
        for node in sorted(label):
            if not adjacency[node]:
                continue
            scores: Dict[str, float] = defaultdict(float)
            for neighbour, weight in adjacency[node]:
                scores[label[neighbour]] += weight
            if not scores:
                continue
            best = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[0][0]
            if label[node] != best:
                label[node] = best
                changed = True
        if not changed:
            break
    groups: Dict[str, List[str]] = defaultdict(list)
    for node, tag in label.items():
        groups[tag].append(node)
    mapping: Dict[str, str] = {}
    for index, members in enumerate(sorted(groups.values(), key=lambda m: (-len(m), sorted(m)[0])), start=1):
        for node in members:
            mapping[node] = f"C{index:02d}"
    return mapping


def annotate_communities(result: Dict[str, Any]) -> Dict[str, Any]:
    """给建图结果补社区号（节点级 + 统计级）。"""
    mapping = detect_communities(result.get("nodes") or [], result.get("edges") or [])
    counts: Dict[str, int] = defaultdict(int)
    for node in result.get("nodes") or []:
        node["community"] = mapping.get(node["id"], "C00")
        counts[node["community"]] += 1
    result.setdefault("stats", {})["communities"] = dict(sorted(counts.items()))
    return result
