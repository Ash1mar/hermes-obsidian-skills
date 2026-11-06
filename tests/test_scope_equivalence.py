"""Conservative scope proof: retain meaning, provenance and legacy policy."""
import copy
from hermes_source_units.scope_equivalence import prove

def pair():
    r={'inspections':[{'source_ref':'original','finding':'source text','qa':'usable'}]}
    c={'support_refs':[{'unit_ref':{'resource_id':'r','revision':'hash'},'span':{'start':1,'end':2}}],
       'finding':'A requires B','applicability':'same scope','conditions':['A AND B'],'exceptions':['except C']}
    return r,c

def test_only_whitespace_equivalence_can_remove_duplicate_scope_trigger():
    a=pair();b=copy.deepcopy(a);b[1]['applicability']='same  scope\n'
    assert prove([a,b])['equivalent']

def test_source_original_conditions_exceptions_and_scope_drift_all_keep_review():
    a=pair()
    for field,value in [('applicability','different scope'),('conditions',['A OR B']),('exceptions',[]),('finding','A may B'),('support_refs',[])]:
        b=copy.deepcopy(a);b[1][field]=value
        assert not prove([a,b])['equivalent']
    b=copy.deepcopy(a);b[0]['inspections'][0]['finding']='different original'
    assert not prove([a,b])['equivalent']
