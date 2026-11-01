"""Уверенность и согласие — интерпретация; независимые основания считает память S19."""


def describe(record, memory):
    data = record.content['interpretation']
    return {'claim': record.text, 'agreement': data['agreement'], 'confidence': data['confidence'],
            'bases': list(record.sources), **memory.support(record.sources),
            'truth_status': 'Интерпретация; уверенность не доказывает истинность.'}


__all__ = ['describe']
