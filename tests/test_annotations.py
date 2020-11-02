from ChildProject.projects import ChildProject, RecordingProfile
from ChildProject.annotations import AnnotationManager
from ChildProject.tables import IndexTable
import pandas as pd
import numpy as np
import os
import pytest
import shutil

@pytest.fixture(scope='function')
def project(request):
    if not os.path.exists("output/annotations"):
        project = ChildProject("examples/valid_raw_data")
        project.import_data("output/annotations")

    project = ChildProject("output/annotations")
    yield project
    
    os.remove("output/annotations/metadata/annotations.csv")
    shutil.rmtree("output/annotations/annotations")
    os.mkdir("output/annotations/annotations")

def test_import(project):
    am = AnnotationManager(project)

    input_annotations = pd.read_csv('examples/valid_raw_data/raw_annotations/input.csv')
    am.import_annotations(input_annotations)
    am.read()
    
    assert am.annotations.shape[0] == input_annotations.shape[0], "imported annotations length does not match input"

    assert all([
        os.path.exists(os.path.join(project.path, 'annotations', f))
        for f in am.annotations['annotation_filename'].tolist()
    ]), "some annotations are missing"

    errors, warnings = am.validate()
    assert len(errors) == 0 and len(warnings) == 0, "malformed annotations detected"

    for dataset in ['eaf', 'textgrid', 'eaf_solis']:
        annotations = am.annotations[am.annotations['set'] == dataset]
        segments = am.get_segments(annotations)
        segments.drop(columns = annotations.columns, inplace = True)

        pd.testing.assert_frame_equal(
            segments.sort_index(axis = 1).sort_values(segments.columns.tolist()).reset_index(drop = True),
            pd.read_csv('tests/truth/{}.csv'.format(dataset)).sort_index(axis = 1).sort_values(segments.columns.tolist()).reset_index(drop = True),
            check_less_precise = True
        )

def test_clipping(project):
    am = AnnotationManager(project)

    input_annotations = pd.read_csv('examples/valid_raw_data/raw_annotations/input.csv')
    am.import_annotations(input_annotations)
    am.read()

    start = 1981
    stop = 1984
    segments = am.get_segments(am.annotations[am.annotations['set'] == 'vtc_rttm'])
    segments = am.clip_segments(segments, start, stop)
    
    assert segments['segment_onset'].between(start, stop).all() and segments['segment_offset'].between(start, stop).all(), "segments not properly clipped"
    assert segments.shape[0] == 2, "got {} segments, expected 2".format(segments.shape[0])
