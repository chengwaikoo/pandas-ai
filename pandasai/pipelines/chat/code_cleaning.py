import ast
import copy
import re
import uuid
from typing import Any, Generator, List, Union

import astor

from pandasai.helpers.path import find_project_root
from pandasai.helpers.skills_manager import SkillsManager
from pandasai.helpers.sql import extract_table_names

from ...connectors import BaseConnector
from ...connectors.sql import SQLConnector
from ...constants import WHITELISTED_BUILTINS, WHITELISTED_LIBRARIES
from ...exceptions import (
    BadImportError,
    ExecuteSQLQueryNotUsed,
    InvalidConfigError,
    MaliciousQueryError,
)
from ...helpers.logger import Logger
from ...helpers.save_chart import add_save_chart
from ...schemas.df_config import Config
from ..base_logic_unit import BaseLogicUnit
from ..logic_unit_output import LogicUnitOutput
from ..pipeline_context import PipelineContext


class CodeExecutionContext:
    def __init__(
        self,
        prompt_id: uuid.UUID,
        skills_manager: SkillsManager,
    ):
        """
        Code Execution Context
        Args:
            prompt_id (uuid.UUID): Prompt ID
            skills_manager (SkillsManager): Skills Manager
        """
        self.skills_manager = skills_manager
        self.prompt_id = prompt_id


class FunctionCallVisitor(ast.NodeVisitor):
    """
    Iterate over the code to find function calls
    """

    def __init__(self):
        self.function_calls = []

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name):
            self.function_calls.append(node.func.id)
        elif isinstance(node.func, ast.Attribute) and isinstance(
            node.func.value, ast.Name
        ):
            self.function_calls.append(f"{node.func.value.id}.{node.func.attr}")
        self.generic_visit(node)


class CodeCleaning(BaseLogicUnit):
    """
    Code Cleaning Stage
    """

    _dfs: List
    _config: Union[Config, dict]
    _logger: Logger = None
    _additional_dependencies: List[dict] = []
    _ast_comparator_map: dict = {
        ast.Eq: "=",
        ast.NotEq: "!=",
        ast.Lt: "<",
        ast.LtE: "<=",
        ast.Gt: ">",
        ast.GtE: ">=",
        ast.Is: "is",
        ast.IsNot: "is not",
        ast.In: "in",
        ast.NotIn: "not in",
    }
    _current_code_executed: str = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._function_call_visitor = FunctionCallVisitor()

    def execute(self, input: Any, **kwargs) -> LogicUnitOutput:
        context: PipelineContext = kwargs.get("context")
        self._dfs = context.dfs
        self._config = context.config
        self._logger = kwargs.get("logger")

        code_to_run = self.get_code_to_run(input, context)

        context.add("additional_dependencies", self._additional_dependencies)
        context.add("current_code_executed", self._current_code_executed)

        return LogicUnitOutput(
            code_to_run,
            True,
            "Code Cleaned Successfully",
        )

    def _replace_plot_png(self, code):
        """
        Replace plot.png with temp_chart.png
        Args:
            code (str): Python code to execute
        Returns:
            str: Python code with plot.png replaced with temp_chart.png
        """
        return re.sub(r"""(['"])([^'"]*\.png)\1""", r"\1temp_chart.png\1", code)

    def get_code_to_run(self, code: str, context: CodeExecutionContext) -> Any:
        code = self._replace_plot_png(code)
        self._current_code_executed = code

        # Add save chart code
        if self._config.save_charts:
            code = add_save_chart(
                code,
                logger=self._logger,
                file_name=str(context.prompt_id),
                save_charts_path_str=self._config.save_charts_path,
            )
        else:
            # Temporarily save generated chart to display
            code = add_save_chart(
                code,
                logger=self._logger,
                file_name="temp_chart",
                save_charts_path_str=f"{find_project_root()}/exports/charts",
            )

        # Reset used skills
        context.skills_manager.used_skills = []

        # Get the code to run removing unsafe imports and df overwrites
        code_to_run = self._clean_code(code, context)
        self._logger.log(
            f"""
Code running:
```
{code_to_run}
        ```"""
        )

        return code_to_run

    def _is_jailbreak(self, node: ast.stmt) -> bool:
        """
        Remove jailbreaks from the code to prevent malicious code execution.
        Args:
            node (ast.stmt): A code node to be checked.
        Returns (bool):
        """

        DANGEROUS_BUILTINS = ["__subclasses__", "__builtins__", "__import__"]

        node_str = ast.dump(node)

        return any(builtin in node_str for builtin in DANGEROUS_BUILTINS)

    def _is_unsafe(self, node: ast.stmt) -> bool:
        """
        Remove unsafe code from the code to prevent malicious code execution.

        Args:
            node (ast.stmt): A code node to be checked.

        Returns (bool):
        """

        code = astor.to_source(node)
        return any(
            (
                method in code
                for method in [
                    ".to_csv",
                    ".to_excel",
                    ".to_json",
                    ".to_sql",
                    ".to_feather",
                    ".to_hdf",
                    ".to_parquet",
                    ".to_pickle",
                    ".to_gbq",
                    ".to_stata",
                    ".to_records",
                    ".to_latex",
                    ".to_html",
                    ".to_markdown",
                    ".to_clipboard",
                ]
            )
        )

    def find_function_calls(self, node: ast.AST, context: CodeExecutionContext):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if context.skills_manager.skill_exists(node.func.id):
                    context.skills_manager.add_used_skill(node.func.id)
            elif isinstance(node.func, ast.Attribute) and isinstance(
                node.func.value, ast.Name
            ):
                context.skills_manager.add_used_skill(
                    f"{node.func.value.id}.{node.func.attr}"
                )

        for child_node in ast.iter_child_nodes(node):
            self.find_function_calls(child_node, context)

    def check_direct_sql_func_def_exists(self, node: ast.AST):
        return (
            self._validate_direct_sql(self._dfs)
            and isinstance(node, ast.FunctionDef)
            and node.name == "execute_sql_query"
        )

    def check_skill_func_def_exists(self, node: ast.AST, context: CodeExecutionContext):
        return isinstance(
            node, ast.FunctionDef
        ) and context.skills_manager.skill_exists(node.name)

    def _validate_direct_sql(self, dfs: List[BaseConnector]) -> bool:
        """
        Raises error if they don't belong sqlconnector or have different credentials
        Args:
            dfs (List[BaseConnector]): list of BaseConnectors

        Raises:
            InvalidConfigError: Raise Error in case of config is set but criteria is not met
        """

        if self._config.direct_sql:
            if all((isinstance(df, SQLConnector) and df.equals(dfs[0])) for df in dfs):
                return True
            else:
                raise InvalidConfigError(
                    "Direct requires all SQLConnector and they belong to same datasource "
                    "and have same credentials"
                )
        return False

    def _replace_table_names(
        self, sql_query: str, table_names: list, allowed_table_names: list
    ):
        regex_patterns = {
            table_name: re.compile(r"\b" + re.escape(table_name) + r"\b")
            for table_name in table_names
        }
        for table_name in table_names:
            if table_name in allowed_table_names.keys():
                quoted_table_name = allowed_table_names[table_name]
                sql_query = regex_patterns[table_name].sub(quoted_table_name, sql_query)
            else:
                raise MaliciousQueryError(
                    f"Query uses unauthorized table: {table_name}."
                )

        return sql_query

    def _validate_and_make_table_name_case_sensitive(self, node: ast.Assign):
        """
        Validates whether table exists in specified dataset and convert name to case-sensitive
        Args:
            node (ast.Assign): code tree node

        Returns:
            node: return updated or same node
        """
        if isinstance(node, ast.Assign):
            # Check if the assigned value is a string constant and the target is 'sql_query'
            if (
                isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in ["sql_query", "query"]
            ):
                sql_query = node.value.value
                table_names = extract_table_names(sql_query)
                allowed_table_names = {df.name: df.cs_table_name for df in self._dfs}
                allowed_table_names |= {
                    f'"{df.name}"': df.cs_table_name for df in self._dfs
                }

                sql_query = self._replace_table_names(
                    sql_query, table_names, allowed_table_names
                )
                node.value.value = sql_query

        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            # Check if the function call is to 'execute_sql_query' and has a string constant argument
            if (
                isinstance(node.value.func, ast.Name)
                and node.value.func.id == "execute_sql_query"
                and len(node.value.args) == 1
                and isinstance(node.value.args[0], ast.Constant)
                and isinstance(node.value.args[0].value, str)
            ):
                sql_query = node.value.args[0].value
                table_names = extract_table_names(sql_query)
                allowed_table_names = {df.name: df.cs_table_name for df in self._dfs}
                allowed_table_names |= {
                    f'"{df.name}"': df.cs_table_name for df in self._dfs
                }
                sql_query = self._replace_table_names(
                    sql_query, table_names, allowed_table_names
                )
                node.value.args[0].value = sql_query

        return node

    def _get_target_names(self, targets):
        target_names = []
        is_slice = False

        for target in targets:
            if isinstance(target, ast.Name) or (
                isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
            ):
                target_names.append(
                    target.id if isinstance(target, ast.Name) else target.value.id
                )
                is_slice = isinstance(target, ast.Subscript)

        return target_names, is_slice, target

    def _check_is_df_declaration(self, node: ast.AST):
        value = node.value
        return (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and isinstance(value.func.value, ast.Name)
            and hasattr(value.func.value, "id")
            and value.func.value.id == "pd"
            and value.func.attr == "DataFrame"
        )

    def _extract_fix_dataframe_redeclarations(
        self, node: ast.AST, code_lines: list[str]
    ) -> ast.AST:
        if isinstance(node, ast.Assign):
            target_names, is_slice, target = self._get_target_names(node.targets)

            if target_names and self._check_is_df_declaration(node):
                # Construct dataframe from node
                code = "\n".join(code_lines)
                env = self._get_environment()
                env["dfs"] = copy.deepcopy(self._dfs)
                exec(code, env)

                df_generated = (
                    env[target_names[0]][target.slice.value]
                    if is_slice
                    else env[target_names[0]]
                )

                # check if exists in provided dfs
                for index, df in enumerate(self._dfs):
                    head = df.get_head()
                    if head.shape == df_generated.shape and head.columns.equals(
                        df_generated.columns
                    ):
                        target_var = (
                            ast.Subscript(
                                value=ast.Name(id=target_names[0], ctx=ast.Load()),
                                slice=target.slice,
                                ctx=ast.Store(),
                            )
                            if is_slice
                            else ast.Name(id=target_names[0], ctx=ast.Store())
                        )
                        return ast.Assign(
                            targets=[target_var],
                            value=ast.Subscript(
                                value=ast.Name(id="dfs", ctx=ast.Load()),
                                slice=ast.Index(value=ast.Num(n=index)),
                                ctx=ast.Load(),
                            ),
                        )
        return None

    def _clean_code(self, code: str, context: CodeExecutionContext) -> str:
        """
        A method to clean the code to prevent malicious code execution.

        Args:
            code(str): A python code.

        Returns:
            str: A clean code string.

        """

        # Clear recent optional dependencies
        self._additional_dependencies = []

        clean_code_lines = []

        tree = ast.parse(code)

        # Check for imports and the node where analyze_data is defined
        new_body = []
        execute_sql_query_used = False

        # find function calls
        self._function_call_vistor.visit(tree)

        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self._check_imports(node)
                continue

            if (
                self._is_df_overwrite(node)
                or self._is_jailbreak(node)
                or self._is_unsafe(node)
            ):
                continue

            # if generated code contain execute_sql_query def remove it
            # function already defined
            if self.check_direct_sql_func_def_exists(node):
                continue

            if self.check_skill_func_def_exists(node, context):
                continue

            # if generated code contain execute_sql_query usage
            if (
                self._validate_direct_sql(self._dfs)
                and "execute_sql_query" in self._function_call_vistor.function_calls
            ):
                execute_sql_query_used = True

            # Sanity for sql query the code should only use allowed tables
            if self._config.direct_sql:
                node = self._validate_and_make_table_name_case_sensitive(node)

            self.find_function_calls(node, context)

            clean_code_lines.append(astor.to_source(node))

            new_body.append(
                self._extract_fix_dataframe_redeclarations(node, clean_code_lines)
                or node
            )

        # Enforcing use of execute_sql_query via Error Prompt Pipeline
        if self._config.direct_sql and not execute_sql_query_used:
            raise ExecuteSQLQueryNotUsed(
                "For Direct SQL set to true, execute_sql_query function must be used. Generating Error Prompt!!!"
            )

        new_tree = ast.Module(body=new_body)
        return astor.to_source(new_tree, pretty_source=lambda x: "".join(x)).strip()

    def _is_df_overwrite(self, node: ast.stmt) -> bool:
        """
        Remove df declarations from the code to prevent malicious code execution.

        Args:
            node (ast.stmt): A code node to be checked.

        Returns (bool):

        """

        return (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "dfs"
        )

    def _check_imports(self, node: Union[ast.Import, ast.ImportFrom]):
        """
        Add whitelisted imports to _additional_dependencies.

        Args:
            node (object): ast.Import or ast.ImportFrom

        Raises:
            BadImportError: If the import is not whitelisted

        """
        module = node.names[0].name if isinstance(node, ast.Import) else node.module
        library = module.split(".")[0]

        if library == "pandas":
            return

        if (
            library
            in WHITELISTED_LIBRARIES + self._config.custom_whitelisted_dependencies
        ):
            for alias in node.names:
                self._additional_dependencies.append(
                    {
                        "module": module,
                        "name": alias.name,
                        "alias": alias.asname or alias.name,
                    }
                )
            return

        if library not in WHITELISTED_BUILTINS:
            raise BadImportError(library)

    @staticmethod
    def _get_nearest_func_call(current_lineno, calls, func_name):
        """
        Utility function to get the nearest previous call node.

        Sort call nodes list (copy of the list) by line number.
        Iterate over the call nodes list. If the call node's function name
        equals to `func_name`, set `nearest_call` to the node object.

        Args:
            current_lineno (int): Number of the current processed line.
            calls (list[ast.Assign]): List of call nodes.
            func_name (str): Name of the target function.

        Returns:
            ast.Call: The node of the nearest previous call `<func_name>()`.
        """
        for call in reversed(calls):
            if call.lineno < current_lineno:
                try:
                    if call.func.attr == func_name:
                        return call
                except AttributeError:
                    continue

        return None

    @staticmethod
    def _tokenize_operand(operand_node: ast.expr) -> Generator[str, None, None]:
        """
        Utility generator function to get subscript slice constants.

        Args:
            operand_node (ast.expr):
                The node to be tokenized.
        Yields:
            str: Token string.

        Examples:
            >>> code = '''
            ... foo = [1, [2, 3], [[4, 5], [6, 7]]]
            ... print(foo[2][1][0])
            ... '''
            >>> tree = ast.parse(code)
            >>> res = CodeManager._tokenize_operand(tree.body[1].value.args[0])
            >>> print(list(res))
            ['foo', 2, 1, 0]
        """
        if isinstance(operand_node, ast.Call):
            yield operand_node.func.attr

        if isinstance(operand_node, ast.Subscript):
            slice_ = operand_node.slice.value
            yield from CodeCleaning._tokenize_operand(operand_node.value)
            yield slice_

        if isinstance(operand_node, ast.Name):
            yield operand_node.id

        if isinstance(operand_node, ast.Constant):
            yield operand_node.value

    @staticmethod
    def _get_df_id_by_nearest_assignment(
        current_lineno: int, assignments: list[ast.Assign], target_name: str
    ):
        """
        Utility function to get df label by finding the nearest assignment.

        Sort assignment nodes list (copy of the list) by line number.
        Iterate over the assignment nodes list. If the assignment node's value
        looks like `dfs[<index>]` and target label equals to `target_name`,
        set `nearest_assignment` to "dfs[<index>]".

        Args:
            current_lineno (int): Number of the current processed line.
            assignments (list[ast.Assign]): List of assignment nodes.
            target_name (str): Name of the target variable. The assignment
                node is supposed to assign to this name.

        Returns:
            str: The string representing df label, looks like "dfs[<index>]".
        """
        nearest_assignment = None
        assignments = sorted(assignments, key=lambda node: node.lineno)
        for assignment in assignments:
            if assignment.lineno > current_lineno:
                return nearest_assignment
            try:
                is_subscript = isinstance(assignment.value, ast.Subscript)
                dfs_on_the_right = assignment.value.value.id == "dfs"
                assign_to_target = assignment.targets[0].id == target_name
                if is_subscript and dfs_on_the_right and assign_to_target:
                    nearest_assignment = f"dfs[{assignment.value.slice.value}]"
            except AttributeError:
                continue
