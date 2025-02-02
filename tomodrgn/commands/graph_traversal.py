"""
Sample latent embeddings along the shortest path through the latent space nearest neighbor graph which connects specified anchor points.
"""
import argparse
import os
from heapq import heappush, heappop
from itertools import pairwise

import matplotlib.pyplot as plt
import numpy as np
from adjustText import adjust_text
from matplotlib.collections import LineCollection
from scipy.spatial import KDTree

from tomodrgn import utils

log = utils.log


def add_args(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    if parser is None:
        # this script is called directly; need to create a parser
        parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    else:
        # this script is called from tomodrgn.__main__ entry point, in which case a parser is already created
        pass

    parser.add_argument('z', type=os.path.abspath,
                        help='Input latent embeddings z.pkl file')

    group = parser.add_argument_group('Core arguments')
    group.add_argument('--anchors', type=int, nargs='+', required=True,
                       help='Indices of anchor points along desired trajectory. At least 2 points must be specified.')
    group.add_argument('-o', '--outdir', type=os.path.abspath, required=True,
                       help='Directory in which to store output .txt/.pkl files of path indices and latent embeddings')
    group.add_argument('--max-neighbors', type=int, default=10,
                       help='The maximum number of neighbors to initially calculate distances for from each latent embedding')
    group.add_argument('--avg-neighbors', type=float, default=None,
                       help='Used to set a cutoff distance defining connected neighbors such that each embedding will have this many connected neighbors on average')
    group.add_argument('--plot-format', type=str, choices=['png', 'svgz'], default='png', help='File format with which to save plots')

    return parser


class LatentGraph:
    """
    Class for describing connected latent embeddings as a graph based on proximity in latent space.
    """

    def __init__(self,
                 edges: list[tuple[int, int, float]]):
        """
        Initialize a LatentGraph object from a list of connected edges.

        :param edges: list of tuples (src, dest, distance)
        """
        self.nodes = set([x[0] for x in edges] + [x[1] for x in edges])
        self.edges = {x: set() for x in self.nodes}
        self.edge_length = {}
        for s, d, L in edges:
            assert type(s) is int and type(d) is int and type(L) is float
            self.edges[s].add(d)
            self.edge_length[(s, d)] = L

    @classmethod
    def construct_from_array(cls,
                             data: np.ndarray,
                             max_neighbors: int,
                             avg_neighbors: int, ):
        """
        Constructor method to create a graph of connected latent embeddings from an array of all latent embeddings.

        :param data: array of latent embeddings, shape (nptcls, zdim)
        :param max_neighbors: maximum number of neighbors to initially calculate distances for from each latent embedding
        :param avg_neighbors: used to set a cutoff distance defining connected neighbors such that each embedding will have this many connected neighbors on average
        :return: LatentGraph instance
        """
        nptcls, zdim = data.shape

        # construct the distance tree
        tree = KDTree(data)
        # query the tree for the max_neighbors nearest points (+1 because query will return self as the closest point in this context)
        dists, neighbors = tree.query(x=data, k=max_neighbors + 1)
        # exclude self from the neighbor results
        dists = dists[:, 1:]
        neighbors = neighbors[:, 1:]
        # calculate the maximum allowable distance to enforce an average of args.avg_neighbors neighbors per particle
        if avg_neighbors:
            total_neighbors = int(nptcls * avg_neighbors)
            max_dist = np.sort(dists.flatten())[total_neighbors - 1]
        else:
            max_dist = None

        log(f'Constructing graph of neighbor particles within distance {max_dist} (to enforce average of {avg_neighbors} neighbors)')
        edges = []
        for i in range(nptcls):
            for j in range(max_neighbors):
                if max_dist is None or dists[i, j] < max_dist:
                    # edges are defined as (idx_particle, idx_neighbor, dist_to_neighbor)
                    edges.append((int(i), int(neighbors[i, j]), float(dists[i, j])))

        return LatentGraph(edges)

    def find_path_dijkstra(self,
                           src: int,
                           dest: int) -> tuple[list[int], float] | tuple[None, None]:
        """
        Standard implementation of Dijkstra's algorithm to find the shortest path through a weighted graph.
        Earliest reference I can find for this code is: https://github.com/theannielin/drkung143/blob/master/q9/dijkstra.py

        :param src: index of starting node
        :param dest: index of ending node
        :return: list of node indices connecting src and dest nodes and total distance of that path, or (None, None) if no path can be found
        """
        visited = set()
        unvisited = []
        distances = {}
        predecessors = {}

        distances[src] = 0
        heappush(unvisited, (0, src))

        while unvisited:
            # visit the neighbors
            dist, v = heappop(unvisited)
            if v in visited or v not in self.edges:
                continue
            visited.add(v)
            if v == dest:
                # We build the shortest path and display it
                path = []
                pred = v
                while pred is not None:
                    path.append(pred)
                    pred = predecessors.get(pred, None)
                return path[::-1], dist

            neighbors = list(self.edges[v])

            for idx, neighbor in enumerate(neighbors):
                if neighbor not in visited:
                    new_distance = dist + self.edge_length[(v, neighbor)]
                    if new_distance < distances.get(neighbor, float('inf')):
                        distances[neighbor] = new_distance
                        heappush(unvisited, (new_distance, neighbor))
                        predecessors[neighbor] = v

        # couldn't find a path
        return None, None

    def plot_graph(self,
                   data: np.ndarray) -> tuple[plt.Figure, plt.Axes]:
        """
        Plot a 2-D graph of the data underlying the LatentGraph object.
        Scatter plot all points; draw line segments between connected graph components.

        :param data: data array from which this graph object was created, shape (nptcls, zdim)
        :return: the created graph figure and its contained axis
        """
        fig, ax = plt.subplots(figsize=(8, 8))

        # plot the latent embeddings as scatter points
        ax.scatter(data[:, 0], data[:, 1], s=1, rasterized=True)

        # plot the graph as lines connecting latent embeddings
        for src, dest in self.edge_length.keys():
            ax.plot(data[[src, dest], 0], data[[src, dest], 1], linewidth=0.5, alpha=0.1, color='black', rasterized=True)

        edges_to_plot = [data[[src, dest], :2] for src, dest in self.edge_length.keys()]
        line_collection = LineCollection(edges_to_plot, linewidths=0.5, alpha=0.1, color='black', rasterized=True)
        ax.add_collection(line_collection)

        ax.set_xlabel('z1')
        ax.set_ylabel('z2')
        ax.set_aspect('equal')
        plt.tight_layout()
        return fig, ax

    def plot_path(self,
                  data: np.ndarray,
                  anchor_inds: list[int],
                  path_inds: list[int]) -> tuple[plt.Figure, plt.Axes]:
        """
        Plot a 2-D graph of the data underlying the LatentGraph object, superimposed by a series of connected paths.
        The connected paths are drawn as red line segments.
        Data (particle) indices along path are annotated, with anchor points defining path search input marked in bold.

        :param data: data array from which this graph object was created, shape (nptcls, zdim)
        :param anchor_inds: list of node indices used as anchors to define start and end of each searched path segment
        :param path_inds: list of node indices defining each minimum-distance path, in order of input anchor indices
        :return: the created graph figure and its contained axis
        """

        # create a base plot of the graph
        fig, ax = self.plot_graph(data=data)

        # plot the path
        ax.plot(data[path_inds, 0], data[path_inds, 1], linewidth=1, alpha=1, color='red', marker='.', linestyle=':')

        # plot the anchor points and points along the path
        annotations = []
        for path_ind in path_inds:
            if path_ind in anchor_inds:
                annotations.append(plt.text(x=float(data[path_ind, 0]),
                                            y=float(data[path_ind, 1]),
                                            s=str(path_ind),
                                            ha='center',
                                            va='center',
                                            color='red',
                                            weight='bold'))
            else:
                annotations.append(plt.text(x=float(data[path_ind, 0]),
                                            y=float(data[path_ind, 1]),
                                            s=str(path_ind),
                                            ha='center',
                                            va='center',
                                            color='red'))
        adjust_text(texts=annotations,
                    expand=(1.5, 1.5),
                    arrowprops=dict(arrowstyle='->', color='black'))

        return fig, ax


def main(args):
    # log args, create output directory
    log(args)
    if not os.path.exists(args.outdir):
        os.makedirs(args.outdir)

    # load the latent embeddings array
    data = utils.load_pkl(args.z)

    # sanity check inputs
    nptcls, zdim = data.shape
    for i in args.anchors:
        assert i < nptcls, f'A particle index in --anchors exceeds the number of particles found in {args.z}: {nptcls}'
    assert len(args.anchors) >= 2, 'At least 2 anchors (beginning and ending particles) required to initialize path search'

    # construct the graph of connected neighbors in latent space (connected meaning Euclidean nearest within a threshold)
    graph = LatentGraph.construct_from_array(data=data,
                                             max_neighbors=args.max_neighbors,
                                             avg_neighbors=args.avg_neighbors)

    # find the shortest path connecting all anchor points
    full_path = []
    for src, dest in pairwise(args.anchors):
        # find the shortest path connecting each sequential pair of anchors
        log(f'Searching for shortest path between anchor points {src} and {dest}')
        path, path_distance = graph.find_path_dijkstra(src, dest)
        dd = data[path]
        dists = ((dd[1:, :] - dd[0:-1, :]) ** 2).sum(axis=1) ** .5

        # report details about the found path
        if path is not None:
            log(f'Found shortest path: {" ".join(str(ind) for ind in path)}')
            log(f"Total path distance: {path_distance}")
            log(f'Distances between each neighbor along path: {" ".join(str(dist) for dist in dists)}')
            log(f'Direct distance between source and destination anchor points: {((dd[0] - dd[-1]) ** 2).sum() ** .5}')
        else:
            log("Could not find path!")

        # extend the overall path between all anchor points
        if path is not None:
            if full_path and full_path[-1] == path[0]:
                # avoid repeating the same node twice
                full_path.extend(path[1:])
            else:
                full_path.extend(path)

    # save outputs
    np.savetxt(fname=os.path.join(args.outdir, 'path_particle_indices.txt'), X=full_path)
    utils.save_pkl(data=full_path, out_pkl=os.path.join(args.outdir, 'path_particle_indices.pkl'), )
    utils.save_pkl(data=data[full_path], out_pkl=os.path.join(args.outdir, 'path_particle_embeddings.pkl'), )

    # make some plots
    log('Plotting graph and path')
    _ = graph.plot_graph(data=data)
    plt.savefig(os.path.join(args.outdir, f'latent_graph.{args.plot_format}'), dpi=300)
    plt.close()
    _ = graph.plot_path(data=data, anchor_inds=args.anchors, path_inds=full_path)
    plt.savefig(os.path.join(args.outdir, f'latent_graph_path.{args.plot_format}'), dpi=300)
    plt.close()

    potential_umap_path = f'{os.path.dirname(args.z)}/analyze.{os.path.basename(args.z).split(".")[1]}/umap.pkl'
    if os.path.isfile(potential_umap_path):
        log('Found umap.pkl, creating additional plots with UMAP embeddings of latent graph for visualization')
        umap = utils.load_pkl(potential_umap_path)
        _ = graph.plot_graph(data=umap)
        plt.savefig(os.path.join(args.outdir, f'umap_graph.{args.plot_format}'), dpi=300)
        plt.close()
        _ = graph.plot_path(data=umap, anchor_inds=args.anchors, path_inds=full_path)
        plt.savefig(os.path.join(args.outdir, f'umap_graph_path.{args.plot_format}'), dpi=300)
        plt.close()


if __name__ == '__main__':
    main(add_args().parse_args())
