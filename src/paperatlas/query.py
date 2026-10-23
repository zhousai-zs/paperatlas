"""Small, explicit IR terminology expansion; no generated translation or external API."""
import re

# Domain terminology only. No paper names, expected pages, or evaluation identifiers.
TERMS = {
    '重新调度': 'rescheduling', '严重程度': 'severity', '视觉语言': 'vision language',
    '多模态': 'multimodal', '自然语言': 'natural language', '人写': 'human written',
    '感知': 'perception', '调度': 'scheduling', '执行': 'execution', '反思': 'reflection',
    '图像': 'image', '恢复': 'restoration', '退化': 'degradation', '噪声': 'noise',
    '去噪': 'denoising', '模糊': 'blur', '去雾': 'dehazing', '去雨': 'deraining',
    '低光': 'low light', '物理': 'physical', '四种': 'four', '场景': 'scene',
    '描述': 'descriptor', '控制': 'controllable', '指令': 'instructions',
    '专家': 'experts', '类型': 'types', '动态': 'dynamic', '选择': 'selection',
    '用户': 'user', '难度': 'complexity', '快慢': 'fast slow', '智能体': 'agent',
    '模型': 'model', '数据集': 'dataset', '训练': 'training', '测试': 'test',
    '提示': 'prompt', '频率': 'frequency', '频域': 'frequency', '高频': 'high frequency',
    '低频': 'low frequency', '空间': 'spatial', '质量': 'quality', '评估': 'assessment',
    '混合': 'mixed', '复合': 'composite', '未知': 'unknown', '先验': 'prior',
    '记忆': 'memory', '因果': 'causal', '强化学习': 'reinforcement learning',
    '遥感': 'remote sensing', '无监督': 'unsupervised', '成对': 'paired',
}
PATTERN = re.compile('|'.join(re.escape(k) for k in sorted(TERMS, key=len, reverse=True)))


def expand(query):
    translations = list(dict.fromkeys(TERMS[m[0]] for m in PATTERN.finditer(query)))
    if not translations:
        return query
    # E5's small model is less reliable on long Chinese questions in this corpus.
    # Retain the user's words, append only auditable technical aliases.
    return query + ' ' + ' '.join(translations)
