"""Add read-only literature actions around the unchanged state/proof broker."""
from __future__ import annotations
from revisions.dual_agent_v1_1.tools import DualAgentTools as FixedTools, _new, _json
from revisions.kimi_adaptive50_web_v1.web_tools import WebBroker

WEB_TOOLS=frozenset({'web_search','web_fetch'})


class NetworkRoleBroker:
    def __init__(self,base,web):
        self.base,self.web=base,web

    def __getattr__(self,name):return getattr(self.base,name)

    def dispatch(self,action,session=None):
        if not isinstance(action,dict) or action.get('tool') not in WEB_TOOLS:
            return self.base.dispatch(action,session)
        try:
            if set(action)!={'tool','arguments'} or not isinstance(action['arguments'],dict):
                raise ValueError('Expected exactly tool and arguments')
            if self.base.role=='formalizer':self.base.owner._assert_frozen(self.base._handoff)
            args=action['arguments'];tool=action['tool']
            invocation=self.base._invocation(tool)
            if tool=='web_search':
                self.base._keys(args,{'query'},{'max_results'})
                response=self.web.search(args['query'],max_results=args.get('max_results',5),invocation_dir=invocation)
            else:
                self.base._keys(args,{'url'},{'max_chars'})
                response=self.web.fetch(args['url'],max_chars=args.get('max_chars',20000),invocation_dir=invocation)
            # External data are nested; never project action/energy/certificate
            # fields from an external document into the host protocol.
            result={'ok':response.get('ok') is True,'tool':tool,'web_result':response,
                    'proof_verified':False,'external_reference_only':True,
                    'untrusted_source_content':True,'evidence_directory':str(invocation)}
            _new(invocation/'host-result.json',_json(result))
            return result
        except Exception as error:
            return {'ok':False,'error':type(error).__name__,'detail':str(error)[:2000],
                    'proof_verified':False,'external_reference_only':True}


class DualAgentTools:
    def __init__(self,*args,web=None,**kwargs):
        self.base=FixedTools(*args,**kwargs)
        self.web=web or WebBroker()
        self.constructor=NetworkRoleBroker(self.base.constructor,self.web)
        self._formalizer_base=None;self._formalizer_wrapper=None

    def __getattr__(self,name):return getattr(self.base,name)

    @property
    def formalizer(self):
        current=self.base.formalizer
        if current is None:return None
        if current is not self._formalizer_base:
            self._formalizer_base=current
            self._formalizer_wrapper=NetworkRoleBroker(current,self.web)
        return self._formalizer_wrapper
