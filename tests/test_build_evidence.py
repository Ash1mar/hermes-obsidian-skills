"""Reference mapping is mechanical; spans and whole registered assets matter."""
import copy
from build_evidence import page_check

def view():
    return {'original_evidence':{'tasks':[{'task':'t1','materials':[{'ref':'m1','text_ref':'c1','source_span':{'start':10,'end':30}}]}],
        'texts':{'c1':'bounded original text'}},
        'citation_evidence':[{'source_evidence':[{'reading':'t1','support_refs':[{'ref':'m1','span':{'start':12,'end':18}}]}]}]}

def test_legal_bounded_subspan_is_delivered():assert page_check(view())['ok']

def test_absent_handle_and_outside_span_are_not_conversion_errors():
    a=view();a['citation_evidence'][0]['source_evidence'][0]['support_refs'][0]['span']['end']=31
    assert not page_check(a)['ok']
    a=view();a['citation_evidence'][0]['source_evidence'][0]['support_refs']=['m2']
    assert page_check(a)['failures']==['candidate support handle not delivered']
