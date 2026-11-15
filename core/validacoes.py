"""Validações de documentos brasileiros e contato."""

import re


def so_digitos(valor: str) -> str:
    return re.sub(r"\D", "", valor or "")


def cpf_valido(cpf: str) -> bool:
    cpf = so_digitos(cpf)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    for tamanho in (9, 10):
        soma = sum(int(cpf[i]) * (tamanho + 1 - i) for i in range(tamanho))
        digito = (soma * 10) % 11 % 10
        if digito != int(cpf[tamanho]):
            return False
    return True


def cnpj_valido(cnpj: str) -> bool:
    cnpj = so_digitos(cnpj)
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        return False
    pesos = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    for tamanho in (12, 13):
        soma = sum(int(cnpj[i]) * pesos[i + (13 - tamanho)] for i in range(tamanho))
        resto = soma % 11
        digito = 0 if resto < 2 else 11 - resto
        if digito != int(cnpj[tamanho]):
            return False
    return True


def documento_valido(doc: str) -> bool:
    d = so_digitos(doc)
    return cpf_valido(d) if len(d) == 11 else cnpj_valido(d) if len(d) == 14 else False


def telefone_valido(tel: str) -> bool:
    t = so_digitos(tel)
    if t.startswith("55") and len(t) in (12, 13):
        t = t[2:]
    return len(t) in (10, 11) and t[0] != "0" and (len(t) == 10 or t[2] == "9")
