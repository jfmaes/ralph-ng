from project.src.example_module import describe


def test_describe_prefixes_value() -> None:
    assert describe("demo") == "example:demo"
