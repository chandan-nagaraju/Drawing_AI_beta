from coordinate_driven_semantic_reconstruction import (
    run_coordinate_driven_semantic_reconstruction,
)


if __name__ == "__main__":
    run_coordinate_driven_semantic_reconstruction(
        pdf_path=r"C:\Users\Lenovo\Downloads\X6C22514.pdf",
        output_path="../outputs/vector_relationships.json",
        versioned_only=True,
    )
    print("Saved new versioned file under ../outputs/")