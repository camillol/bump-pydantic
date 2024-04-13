from __future__ import annotations

import libcst as cst
import libcst.matchers as m
from libcst.codemod import CodemodContext, VisitorBasedCodemodCommand
from libcst.codemod.visitors import AddImportsVisitor
from libcst.metadata import FullyQualifiedNameProvider, NonCachedTypeInferenceProvider

from bump_pydantic.codemods.class_def_visitor import ClassDefVisitor

PREFIX_COMMENT = "# TODO[pydantic]: "
REFACTOR_COMMENT = (
    f"{PREFIX_COMMENT}all model fields must have a type annotation."
)

UNTYPED_ASSIGN_MATCHER = m.Assign(targets=[m.AssignTarget(m.Name() & ~m.Name("model_config"))])
MEMBER_ASSIGN_ANCESTORS = [m.ClassDef(), m.IndentedBlock(), m.SimpleStatementLine()]

class AddMissingAnnotationCommand(VisitorBasedCodemodCommand):

    METADATA_DEPENDENCIES = (FullyQualifiedNameProvider, NonCachedTypeInferenceProvider)

    def __init__(self, context: CodemodContext) -> None:
        super().__init__(context)

        self.pydantic_model_bases = self.context.scratch[ClassDefVisitor.BASE_MODEL_CONTEXT_KEY].known_members
        self.should_add_comment = False
        self.node_stack = list[cst.CSTNode]()

    def on_visit(self, node: cst.CSTNode) -> bool:
        self.node_stack.append(node)
        return super().on_visit(node)

    def on_leave(self, original_node: cst.CSTNode, updated_node: cst.CSTNode) -> cst.CSTNode | cst.RemovalSentinel:
        self.node_stack.pop()
        return super().on_leave(original_node, updated_node)

    def _is_pydantic_model(self, node: cst.CSTNode) -> bool:
        fqn_set = self.get_metadata(FullyQualifiedNameProvider, node, set())
        return any(fqn.name in self.pydantic_model_bases for fqn in fqn_set)

    @m.leave(UNTYPED_ASSIGN_MATCHER)
    def leave_untyped_member_assign(self, original_node: cst.Assign, updated_node: cst.Assign) -> cst.Assign | cst.AnnAssign:
        ancestors = self.node_stack[-3:]
        if len(ancestors) < 3 or not self._is_pydantic_model(ancestors[0]) or not all(
            m.matches(parent, matcher) for parent, matcher in zip(self.node_stack[-3:], MEMBER_ASSIGN_ANCESTORS, strict=True)):
            return updated_node

        annotation = None
        if m.matches(updated_node.value, m.SimpleString()):
            annotation = cst.Name("str")
        elif m.matches(updated_node.value, m.Integer()):
            annotation = cst.Name("int")
        elif m.matches(updated_node.value, m.Name("True") | m.Name("False")):
            annotation = cst.Name("bool")
        elif m.matches(updated_node.value, m.Float()):
            annotation = cst.Name("float")
        elif (fqn := self.get_metadata(NonCachedTypeInferenceProvider, original_node.targets[0].target, None)) and fqn != "typing.Any":
            try:
                annotation = cst.parse_expression(fqn)
                root_attr_matcher = m.Attribute(value=m.Name(), attr=m.Name())
                for attribute in m.findall(annotation, root_attr_matcher):
                    AddImportsVisitor.add_needed_import(self.context, attribute.value.value)
            except cst.ParserSyntaxError:
                pass

        if annotation is None:
            self.should_add_comment = True
            return updated_node

        return cst.AnnAssign(
            target=updated_node.targets[0].target,
            annotation=cst.Annotation(
                annotation=annotation
            ),
            value=updated_node.value
        )

    def leave_SimpleStatementLine(self, original_node: cst.SimpleStatementLine, updated_node: cst.SimpleStatementLine) -> cst.SimpleStatementLine:
        if self.should_add_comment:
            self.should_add_comment = False
            return updated_node.with_changes(
                leading_lines=[
                    *updated_node.leading_lines,
                    cst.EmptyLine(comment=cst.Comment(value=REFACTOR_COMMENT)),
                ]
            )
        return updated_node
