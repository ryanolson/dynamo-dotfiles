{
  description = "Personal development environment (imports Dynamo team base)";
  
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };
  
  outputs = { nixpkgs, home-manager, ... }: {
    homeConfigurations.default = home-manager.lib.homeManagerConfiguration {
      pkgs = nixpkgs.legacyPackages.x86_64-darwin; # Change to x86_64-linux for Linux  
      modules = [ ./home.nix ];
    };
  };
}