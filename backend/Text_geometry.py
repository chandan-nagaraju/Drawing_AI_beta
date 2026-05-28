from coordinate_driven_semantic_reconstruction import (
    run_coordinate_driven_semantic_reconstruction,
)


if __name__ == "__main__":
    run_coordinate_driven_semantic_reconstruction(
        pdf_path="../F5C04213.pdf",
        output_path="../outputs/vector_relationships.json",
    )
    print("Saved outputs/vector_relationships.json")