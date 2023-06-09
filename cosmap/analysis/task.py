from pydantic import BaseModel
from dask.distributed import get_worker
from dask import delayed
import numpy as np
import math
from types import ModuleType
import networkx as nx
from cosmap.analysis import utils
from functools import partial
from cosmap import analysis
from loguru import logger
from astropy.coordinates import SkyCoord
from devtools import debug

def generate_tasks(client, parameters: BaseModel, dependency_graph: nx.DiGraph, needed_dtypes: list, samples: list, chunk_size: int = 10, plugins = {}):
    """
    

    chunk_size breaks up the computation such that results will be written to disk.
    
    
    """
    if "task_generator" in plugins:
        if len(plugins["task_generator"]) > 1:
            raise Exception("Found multiple task generator plugins! Only one is allowed.")
        plugin_name, plugin_data = list(plugins["task_generator"].items())[0]
        plugin_object = plugin_data["plugin"]
        plugin_parameters = {"client": client, "parameters": parameters, "dependency_graph": dependency_graph, "needed_dtypes": needed_dtypes, "samples": samples, "chunk_size": chunk_size}
        plugin_parameters.update(plugin_data["parameters"])
        g = plugin_object(**plugin_parameters)
        for t in g:
            yield t    
    pipeline_function = build_pipeline(parameters, dependency_graph)
    n_chunks = math.ceil(len(samples) / chunk_size)
    n_workers = len(client.nthreads())
    chunks = np.array_split(samples, n_chunks)
    sample_shape = parameters.sampling_parameters.sample_shape
    sample_dimensions = parameters.sampling_parameters.sample_dimensions

    if sample_shape != "Circle":
        raise NotImplementedError("Only circular samples are currently supported")
    for c in chunks:
        splits = np.array_split(c, n_workers)
        f = partial(main_task, dtypes = needed_dtypes, sample_shape = "cone", sample_dimensions = max(sample_dimensions), pipeline_function = pipeline_function)
        tasks = client.map(f, splits)
        yield tasks


def build_pipeline(parameters: BaseModel, dependency_graph):
    """
    Build the pipeline that will actually run the analysis for a single
    iteration. In essence, we just chain all the invidual tasks together
    to form a pipeline.
    """

    transformations = parameters.analysis_parameters.transformations["Main"]
    transformation_defs = parameters.analysis_definition.transformations.Main
    task_order = list(nx.topological_sort(dependency_graph))
    if not transformations[task_order[-1]].get("is-output"):
        raise Exception("The last task in the pipeline must be an output task!")
    elif any([transformations[t].get("output") for t in task_order[:-1]]):
        raise Exception("Only the last task in the pipeline can be an output task!")
    #We can't pass pydantic objects, so we grab the parameters here
    param_dictionary = parameters.model_dump()
    analysis_param_dictionary = parameters.analysis_parameters.model_dump()
    param_dictionary.update({"analysis_parameters": analysis_param_dictionary})
    #There's a bug in the current beta version of pydantic... Working around it
    param_dictionary.pop("analysis_definition")

    pipeline_function = partial(
        pipeline,
        parameters = param_dictionary,
        transformations = transformations,
        transformation_definitions = transformation_defs,
        task_order = task_order
    )
    return pipeline_function    

def main_task(coordinates, sample_shape, sample_dimensions, dtypes, pipeline_function, *args, **kwargs):
    worker = get_worker()
    dataset = worker.dataset
    sample_generator = dataset.sample_generator(coordinates, dtypes = dtypes, sample_type = sample_shape, sample_dimensions = sample_dimensions)
    results = []
    for region, sample in sample_generator:
        try:
            results.append(pipeline_function(data = sample, sample_region = region))
        except analysis.CosmapBadSampleError:
            logger.warning("Bad sample detected. Skipping...")
            continue
    return results


def pipeline(data: dict, sample_region: SkyCoord, parameters: dict, transformations: dict, transformation_definitions: ModuleType, task_order: list):
    outputs = {}

    for task in task_order:
        needed_data = transformations[task].get("needed-data", [])
        inputs = {n: data[n] for n in needed_data}
        needed_parameters = utils.get_task_parameters_from_dictionary(parameters, "Main", task, outputs)
        inputs.update(needed_parameters)
        inputs.update({"sample_region": sample_region})
        result = getattr(transformation_definitions, task)(**inputs)
        outputs.update({task: result})
    return outputs[task_order[-1]]