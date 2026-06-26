import json
import tempfile
import unittest
from pathlib import Path

import impact_resolver as resolver


class DotnetRouteDiscoveryTests(unittest.TestCase):
    def test_minimal_api_nested_groups_are_joined(self):
        text = """
        var v1 = app.MapGroup("/v1");
        var users = v1.MapGroup("/users");
        users.MapGet("/{id:int}", Handler);
        app.MapPost("/health", Handler);
        """

        routes = resolver.minimal_api_routes(text, resolver.DEFAULTS)

        self.assertEqual({"/v1/users/{id:int}", "/health"}, routes)

    def test_controller_attribute_routes_expand_tokens(self):
        text = """
        [ApiController]
        [Route("api/[controller]")]
        public sealed class OrdersController : ControllerBase
        {
            [HttpGet("{id:int}")]
            public Task<Order> GetById(int id) => default!;

            [HttpPost]
            public IActionResult Create() => Ok();
        }
        """

        routes = resolver.controller_routes(text)

        self.assertEqual(
            {"/api/Orders/{id:int}", "/api/Orders"},
            routes,
        )

    def test_controller_route_matches_server_relative_openapi_path(self):
        cfg = dict(resolver.DEFAULTS)
        spec = {
            "paths": {
                "/orders": {"post": {"tags": ["Orders"]}},
                "/orders/{id}": {"get": {"tags": ["Orders"]}},
            }
        }
        controller = """
        [ApiController]
        [Route("api/[controller]")]
        public sealed class OrdersController : ControllerBase
        {
            [HttpGet("{id:int}")]
            public IActionResult GetById(int id) => Ok();

            [HttpPost]
            public IActionResult Create() => Ok();
        }
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "OrdersController.cs"
            source.write_text(controller, encoding="utf-8")
            spec_file = root / "openapi.json"
            spec_file.write_text(json.dumps(spec), encoding="utf-8")

            info = resolver.spec_paths(spec_file, cfg)
            index, _ = resolver.build_index(
                root, ["OrdersController.cs"], info, cfg)

        self.assertEqual(
            {"/orders", "/orders/{id}"},
            index["OrdersController.cs"],
        )

    def test_suffix_matching_keeps_group_prefix_without_wildcard_overmatch(self):
        cfg = dict(resolver.DEFAULTS)
        norm_to_paths = {
            "/orders/{}": {"/orders/{id}"},
            "/users/{}": {"/users/{id}"},
        }
        suffix_index = resolver.build_suffix_index(norm_to_paths)

        prefixed = resolver.route_hits(
            {"/api/orders/{id:int}"},
            norm_to_paths,
            cfg,
            suffix_index,
        )
        wildcard_only = resolver.route_hits(
            {"/{id:int}"},
            norm_to_paths,
            cfg,
            suffix_index,
        )

        self.assertEqual({"/orders/{id}"}, prefixed)
        self.assertEqual(set(), wildcard_only)


class DotnetReferenceIndexTests(unittest.TestCase):
    def test_source_walk_prunes_dotnet_build_directories(self):
        cfg = dict(resolver.DEFAULTS)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "Visible.cs").write_text(
                "public class Visible {}", encoding="utf-8")
            (root / "src" / "obj").mkdir()
            (root / "src" / "obj" / "Generated.cs").write_text(
                "public class Generated {}", encoding="utf-8")

            sources = resolver.walk_sources(root, cfg)

        self.assertEqual(["src/Visible.cs"], sources)

    def test_changed_implementation_reaches_interface_endpoint(self):
        cfg = dict(resolver.DEFAULTS)
        texts = {
            "CommandOrders.cs": "public class CommandOrders : ICommandOrders {}",
            "OrdersEndpoints.cs": (
                'app.MapPost("/orders", (ICommandOrders orders) => orders.Create());'
            ),
        }
        file_to_paths = {"OrdersEndpoints.cs": {"/orders"}}
        refs, declarations, implementations = resolver.build_reference_index(texts, cfg)

        impacted = resolver.source_impact(
            ["CommandOrders.cs"],
            file_to_paths,
            texts,
            cfg,
            refs,
            declarations,
            implementations,
        )

        self.assertIn("/orders", impacted)
        self.assertTrue(any("ICommandOrders" in reason
                            for reason in impacted["/orders"]))


class RoslynSemanticIndexTests(unittest.TestCase):
    def test_semantic_index_maps_routes_and_operation_ids(self):
        cfg = dict(resolver.DEFAULTS)
        info = {
            "/orders": {
                "norm": "/orders",
                "tag": "Orders",
                "operation_ids": {"CreateOrder"},
            },
            "/orders/{id}": {
                "norm": "/orders/{}",
                "tag": "Orders",
                "operation_ids": {"GetOrder"},
            },
        }
        index = {
            "schema_version": 2,
            "engine": "roslyn",
            "endpoint_files": {
                "OrdersEndpoints.cs": [
                    {
                        "route": "/api/orders/{orderId:int}",
                        "operation_id": "GetOrder",
                        "dependencies": ["IOrderQueries.cs"],
                    },
                    {
                        "route": "",
                        "operation_id": "CreateOrder",
                        "dependencies": ["IOrderCommands.cs"],
                    },
                ],
            },
            "reverse_dependencies": {
                "OrderService.cs": ["OrdersEndpoints.cs"],
            },
            "diagnostics": [],
            "stats": {
                "source_files": 3,
                "dependency_edges": 1,
                "endpoints": 2,
                "unresolved_routes": 0,
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            index_file = Path(tmp) / "semantic.json"
            index_file.write_text(json.dumps(index), encoding="utf-8")
            file_to_paths, endpoint_deps, reverse, diagnostics, stats = (
                resolver.load_semantic_index([index_file], info, cfg))

        self.assertEqual(
            {"/orders", "/orders/{id}"},
            file_to_paths["OrdersEndpoints.cs"],
        )
        self.assertEqual(
            {"OrdersEndpoints.cs"},
            reverse["OrderService.cs"],
        )
        self.assertEqual(
            {("OrdersEndpoints.cs", "/orders")},
            endpoint_deps["IOrderCommands.cs"],
        )
        self.assertEqual([], diagnostics)
        self.assertEqual(2, stats["declared_endpoints"])

    def test_semantic_impact_walks_reverse_symbol_graph(self):
        cfg = dict(resolver.DEFAULTS)
        file_to_paths = {
            "OrdersEndpoints.cs": {"/orders", "/orders/{id}"},
        }
        reverse = {
            "OrderRepository.cs": {"OrderService.cs"},
            "OrderService.cs": {"IOrderService.cs"},
            "IOrderService.cs": {"OrdersEndpoints.cs"},
        }

        impacted = resolver.semantic_source_impact(
            ["OrderRepository.cs"], file_to_paths, reverse, cfg)

        self.assertEqual({"/orders", "/orders/{id}"}, set(impacted))
        self.assertTrue(all(
            "semantic dependency on OrderRepository.cs" in next(iter(reasons))
            for reasons in impacted.values()
        ))

    def test_semantic_impact_does_not_use_similar_type_names(self):
        cfg = dict(resolver.DEFAULTS)
        file_to_paths = {
            "OrdersEndpoints.cs": {"/orders"},
            "PreOrdersEndpoints.cs": {"/pre-orders"},
        }
        reverse = {
            "Order.cs": {"OrdersEndpoints.cs"},
            "PreOrder.cs": {"PreOrdersEndpoints.cs"},
        }

        impacted = resolver.semantic_source_impact(
            ["Order.cs"], file_to_paths, reverse, cfg)

        self.assertEqual({"/orders"}, set(impacted))

    def test_semantic_impact_selects_handler_not_whole_endpoint_file(self):
        cfg = dict(resolver.DEFAULTS)
        file_to_paths = {
            "OrdersEndpoints.cs": {"/orders", "/orders/{id}"},
        }
        endpoint_dependencies = {
            "IOrderCommands.cs": {("OrdersEndpoints.cs", "/orders")},
            "IOrderQueries.cs": {("OrdersEndpoints.cs", "/orders/{id}")},
        }
        reverse = {
            "OrderCommands.cs": {"IOrderCommands.cs"},
        }

        impacted = resolver.semantic_source_impact(
            ["OrderCommands.cs"],
            file_to_paths,
            reverse,
            cfg,
            endpoint_dependencies,
        )

        self.assertEqual({"/orders"}, set(impacted))


if __name__ == "__main__":
    unittest.main()
