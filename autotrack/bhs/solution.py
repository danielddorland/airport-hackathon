import json
import os

# Global cache to ensure we only parse the layout file once
_CONVEYOR_GRAPH = None

def _build_graph():
    """Parses network_layout.json and builds a static adjacency list 
    representing valid node-to-node connections."""
    global _CONVEYOR_GRAPH
    if _CONVEYOR_GRAPH is not None:
        return _CONVEYOR_GRAPH

    # Adjust path to find the file reliably relative to execution context
    layout_path = os.path.join(os.path.dirname(__file__), "data", "network_layout.json")
    
    with open(layout_path, "r") as f:
        layout_data = json.load(f)
        
    graph = {}
    
    # Initialize adjacency sets for all declared nodes
    for node in layout_data["nodes"]:
        graph[node["id"]] = set()
        
    # Populate directed connections based on network edges
    for edge in layout_data["edges"]:
        u, v = edge["from"], edge["to"]
        graph[u].add(v)
        
        # If the edge is bidirectional, add the parallel reverse connection
        if edge.get("bidirectional", False):
            graph[v].add(u)
            
    _CONVEYOR_GRAPH = graph
    return _CONVEYOR_GRAPH

def route_bag(bag_id, current_node, destination_gate, active_faults, edge_occupancy):
    """
    An unoptimized, baseline routing engine. Finds the shortest topological path 
    (fewest number of conveyor belts) to the destination gate using BFS.
    
    Ignores active jams and congestion surcharges.
    """
    # 1. Ensure our map layout is loaded into memory
    graph = _build_graph()
    
    # Safety Check: If already at destination or node doesn't exist, stand still
    if current_node == destination_gate or current_node not in graph:
        return current_node
        
    # 2. Standard Breadth-First Search (BFS) to find path to target gate
    queue = [[current_node]]
    visited = {current_node}
    
    while queue:
        path = queue.pop(0)
        node = path[-1]
        
        # Target found! Return the immediate next step out of our current node
        if node == destination_gate:
            return path[1]
            
        for neighbor in graph.get(node, []):
            if neighbor not in visited:
                visited.add(neighbor)
                new_path = list(path)
                new_path.append(neighbor)
                queue.append(new_path)
                
    # Fallback: If no path exists, stay put to avoid throwing an invalid step error
    return current_node