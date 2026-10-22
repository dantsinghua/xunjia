"""Price questions keep the existing complete meaning contract, never supplement format."""
import pytest
from dify.negotiation_v22.prompts import select_system_prompt
from dify.negotiation_v22.slot_wire import decode_slot_observation

def context():
 return {'schema_version':'2.2','stage':'analysis','members':[{'member_id':'member-a','bond_code':'SYN-A','context':[],'quotes':{},'our_side':'BUY'}],
         'messages':[{'message_id':'in-1','direction':'in','text':'SYN-A 能3.10出500万吗？','historical':False}]}

def test_question_specific_instruction_names_all_four_fields():
 head=select_system_prompt(context(),'analysis').split('\n',1)[0]
 assert 'QUESTION' in head
 assert all(field in head for field in ('intent','validity','negotiation_space','reason'))
 assert '不得只给intent和reason' in head

def test_incomplete_model_meaning_is_still_rejected():
 observation={'member_id':'member-a','source_ids':['in-1'],'side':'ofr','slots':{},'meaning':{'intent':'QUESTION','reason':'synthetic question'}}
 with pytest.raises(ValueError,match='Meaning needs one explicit'):
  decode_slot_observation(observation,context())