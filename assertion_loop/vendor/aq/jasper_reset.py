"""Encode scalar reset pins; an SVA disable condition is not a Jasper operand.

Only a disjunction of pin literals is translated. This declares the pins jointly
active for initialization and all inactive for proof. It is not an arbitrary
Boolean-to-reset compiler. Original SVA and assumptions are never rewritten.
"""
from __future__ import annotations
import re


def _ungroup(text):
    text=text.strip()
    while text.startswith('(') and text.endswith(')'):
        depth=0
        for index,char in enumerate(text):
            depth+=(char=='(')-(char==')')
            if depth==0: break
        if index!=len(text)-1: break
        text=text[1:-1].strip()
    return text


def reset_pins(expression):
    """Return native literal strings, rejecting unsupported/contradictory forms."""
    expression=str(expression or '').strip()
    if not expression: return []
    def terms(text):
        text=_ungroup(text);depth=0;start=0;parts=[];index=0
        while index<len(text):
            char=text[index]
            if char=='(':depth+=1
            elif char==')':depth-=1
            if depth<0: raise ValueError('Unbalanced reset parentheses')
            if depth==0 and text[index:index+2]=='||':
                parts.extend(terms(text[start:index]));index+=2;start=index;continue
            index+=1
        if depth:raise ValueError('Unbalanced reset parentheses')
        if parts:return parts+terms(text[start:])
        return [text]
    name=r'[A-Za-z_$][A-Za-z0-9_$]*(?:\[\d+\])?(?:\.[A-Za-z_$][A-Za-z0-9_$]*(?:\[\d+\])?)*'
    result=[];levels={}
    for term in terms(expression):
        term=_ungroup(term);inverted=False
        if term.startswith(('!','~')):
            inverted=True;term=_ungroup(term[1:])
        if re.fullmatch(name,term):pin=term;active=not inverted
        else:
            match=re.fullmatch(r'('+name+r')\s*==\s*(1\s*\x27[bB][01]|[01])',term)
            if not match:raise ValueError('Unsupported Jasper reset declaration: '+expression+
                '. Expected a reset pin, an inverted pin, or OR-separated pin literals; the original disable iff is unchanged.')
            pin=match[1];active=(match[2][-1]=='1') != inverted
        if pin in levels and levels[pin]!=active:
            raise ValueError('Reset pin has contradictory active levels: '+pin)
        if pin not in levels:result.append(pin if active else '!'+pin)
        levels[pin]=active
    return result


def reset_command(expression):
    pins=reset_pins(expression)
    return 'reset -expression {'+' '.join(pins)+'}' if pins else 'reset -none'
