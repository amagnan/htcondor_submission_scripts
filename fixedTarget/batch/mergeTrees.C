// File: merge_ttrees.C
#include <cmath>
#include <iostream>
#include <sstream>
#include <fstream>
//#include <boost/algorithm/string.hpp>

#include "TFile.h"
#include "TTree.h"
#include "TChain.h"

int mergeTrees() {
    //beam parameters
    unsigned smear = 0;
    unsigned paint = 0;
    unsigned xoff = 50;
    unsigned sigma = 16;
    unsigned ecut = 10;
    std::string prod = "251106";
    std::string eosdir="/eos/experiment/ship/user/ammagnan/TargetProd/";

    for (unsigned iS(0); iS<11; ++iS){
      unsigned myxoff = xoff+iS*sigma;
      std::ostringstream savestring;
      savestring << "smear" << smear << "_paint" << paint << "_xoffset" << myxoff;

      std::cout << " -- Processing " << savestring.str() << std::endl;
      
      // Create a chain and add multiple ROOT files
      TChain chain("cbmsim");  // "myTree" = name of the TTree inside each file
      std::ostringstream filename;
      filename << eosdir << prod << "/" << savestring.str() << "/pythia8_evtgen_Geant4_";// << iR << "_" << ecut << ".0.root";
      //for (unsigned iR(0); iR<nRuns; ++iR){
      chain.Add((filename.str()+"*.root").c_str());

      std::cout << " -- Chain has " << chain.GetEntries() << " entries: ";
      // You can also use wildcards:
      // chain.Add("data/run*.root");
    
      // Create output file
      TFile *fout = new TFile((savestring.str()+"_allRuns.root").c_str(), "RECREATE");
      if (!fout) {
	std::cout << " Problem creating output file" << std::endl;
	return 1;
      }
      // Merge all TTrees into a single TTree
      TTree *newTree = chain.CloneTree(-1, "fast");  // -1 = all entries, "fast" = faster copy (no data compression handling)
      if (!newTree) {
	std::cout << " Problem creating output tree" << std::endl;
	return 1;
      }

      std::cout << " -- Creating new tree with " << newTree->GetEntries() << " entries: " << fout->GetName() << std::endl;
      // Write the merged tree to the new file
      newTree->Write();
      
      // Clean up
      fout->Close();
      delete fout;
    }
    return 1;
}//main
