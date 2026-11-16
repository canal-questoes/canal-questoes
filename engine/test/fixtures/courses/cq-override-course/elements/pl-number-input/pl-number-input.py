# Elemento do curso com o mesmo nome de um elemento core suportado (fixture do motor).
import lxml.html
import prairielearn as pl


def prepare(element_html, data):
    element = lxml.html.fragment_fromstring(element_html)
    name = pl.get_string_attrib(element, "answers-name")
    data["answers_names"][name] = True


def render(element_html, data):
    return "<p>campo sobrescrito pelo curso</p>"
