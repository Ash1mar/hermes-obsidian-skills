"""Remove only exactly identical redundant entries from a new authored Pass."""
import copy
from .validation import fingerprint

def normalize_pass(item):
    result=copy.deepcopy(item);removed=0
    def unique(values):
        nonlocal removed
        seen=set();out=[]
        for value in values:
            identity=fingerprint(value)
            if identity in seen:removed+=1
            else:seen.add(identity);out.append(value)
        return out
    for candidate in result.get('candidates',[]):
        candidate['support_refs']=unique(candidate.get('support_refs',[]))
    for key in ('inspections','candidates'):
        if key in result:result[key]=unique(result[key])
    return result,removed
